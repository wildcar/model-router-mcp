"""Per-request cost computation.

Precedence:
  1. provider-reported cost (e.g. claude CLI total_cost_usd) -> used verbatim
  2. amortized subscription (hook) -> monthly_cost / monthly_quota_units * units
  3. per-request -> from registry prices x usage, by billing group

Prices follow OpenRouter conventions: USD per token (chat), USD per image,
USD per second (audio), USD per character (TTS), plus USD per second of the
provider's own compute for the hosted models that bill that way. Arithmetic uses
Decimal to avoid float drift.

This module deliberately knows nothing about `Task` (ADR-0006). It takes the
coarse billing group, and its callers pass `task.billing_group` — which is why
log rows written before tasks existed price exactly as they did.

Two rules the rest of the system leans on (§7 of docs/UI_CONCEPT_v2.md):

* every number comes back with a `cost_source` saying how much to trust it, and
  "нет методики" is one of those states rather than a zero;
* the sum arrives with its terms, each carrying its own rate, so a screen can
  print the arithmetic instead of a total the operator has to believe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from router_mcp.domain.messages import Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel
from router_mcp.storage.database import SubscriptionRecord


@dataclass(slots=True)
class CostResult:
    cost_usd: float
    # "computed"  — from a price whose origin is known (API/page/admin);
    # "estimated" — from a guessed price nobody confirmed, so the number is
    #               indicative only and must read that way in logs and reports;
    # "provider"  — the provider named the sum and it was charged;
    # "provider_notional" — the provider named the sum but the work went against
    #               a subscription, so no money moved. Kept apart from real
    #               money in every total;
    # "unpriced"  — there is no rate for what this request consumed. Not zero:
    #               zero is a claim, this is a gap;
    # "amortized" — spread across a subscription pool.
    cost_source: str
    pricing_mode: str  # "per_request" | "amortized_subscription"
    # The sum by its terms: [{units, rate, kind, label, amount}]. The inspector
    # prints these so a price can be checked rather than trusted.
    breakdown: list[dict[str, Any]] = field(default_factory=list)


def _d(value: float | int | None) -> Decimal:
    return Decimal(str(value or 0))


def _term(kind: str, label: str, units: float | int | None, rate: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "units": units or 0,
        "rate": rate,
        "amount": float(_d(units) * _d(rate)),
    }


def compute_cost(
    model: RegistryModel,
    modality: Modality,
    usage: Usage,
    *,
    override_usd: float | None = None,
    subscription: SubscriptionRecord | None = None,
    notional: bool = False,
) -> CostResult:
    # 1. Provider-reported cost. Under a subscription the CLI still reports what
    # the work would have cost; that number is real information and fake money,
    # so it is labelled and kept out of the real total.
    if override_usd is not None:
        source = "provider_notional" if notional else "provider"
        return CostResult(
            float(override_usd), source, model.pricing_mode,
            [_term("total", "сумма от поставщика", 1, float(override_usd))],
        )

    # 2. Amortized subscription (design hook).
    if model.pricing_mode == "amortized_subscription" and subscription is not None:
        units = _units_for_modality(modality, usage)
        quota = subscription.monthly_quota_units
        if quota and quota > 0:
            rate = _d(subscription.monthly_cost) / _d(quota)
            amortized = rate * _d(units)
            return CostResult(
                float(amortized), "amortized", "amortized_subscription",
                [_term("amortized", subscription.name, units, float(rate))],
            )
        # No quota -> cannot attribute per-request; record zero but flag mode.
        return CostResult(0.0, "amortized", "amortized_subscription")

    # 3. Per-request from registry prices.
    if (
        not model.is_free
        and not model.has_rate_for(modality)
        and _consumed_anything(modality, usage)
    ):
        # The request used something and nobody ever established a rate for it.
        # Reporting 0.0 here is what made image, stt and tts bill at exactly
        # nothing. A row that is free *as a fact* — every rate zero, provenance
        # confirmed — falls through to the arithmetic below, which correctly
        # yields zero.
        #
        # The test is `is_free`, not `price_is_verified`: a row can carry a
        # confirmed rate for one group and none for another (a per-second-of-audio
        # price says nothing about seconds of GPU), and trusting the provenance
        # alone turned that gap into a confident zero.
        return CostResult(0.0, "unpriced", "per_request")

    terms = _terms(model, modality, usage)
    total = sum((_d(t["amount"]) for t in terms), Decimal(0))
    # A price with no confirmed origin yields a number that only looks precise,
    # and so does a cache rate we had to guess at.
    guessed_cache = any(t.get("assumed") for t in terms)
    source = "computed" if model.price_is_verified and not guessed_cache else "estimated"
    return CostResult(float(total), source, "per_request", terms)


def _terms(model: RegistryModel, modality: Modality, usage: Usage) -> list[dict[str, Any]]:
    # Per second of the provider's own compute. Checked first because it is the
    # only rate some groups have (generated music, generated video), and because
    # a provider that names a per-unit price has already said how it bills — so a
    # per-unit rate, when one exists, wins below.
    if not model.has_unit_rate_for(modality) and (
        model.compute_price_per_second > 0.0 and usage.compute_seconds
    ):
        return [
            _term(
                "compute",
                "секунды расчёта",
                usage.compute_seconds,
                model.compute_price_per_second,
            )
        ]
    if modality is Modality.IMAGE:
        return [_term("image", "изображения", usage.images or 1, model.image_price)]
    if modality is Modality.STT:
        return [_term("audio", "секунды аудио", usage.audio_seconds, model.audio_price_per_second)]
    if modality is Modality.TTS:
        # Per character when the rate is known: ElevenLabs bills the text, not
        # the length of the speech it produced.
        if model.character_price > 0.0 and usage.characters:
            return [_term("characters", "символы", usage.characters, model.character_price)]
        return [_term("audio", "секунды аудио", usage.audio_seconds, model.audio_price_per_second)]
    if modality in (Modality.AUDIO, Modality.VIDEO):
        # Explicit, so these never fall through to the token arithmetic below:
        # a generated clip has no prompt and no completion, and pricing it by
        # tokens would quietly produce zero.
        return [
            _term(
                "compute",
                "секунды расчёта",
                usage.compute_seconds,
                model.compute_price_per_second,
            )
        ]

    # chat and cli: prompt, cache and completion, each at its own rate.
    cache_read = usage.cache_read_tokens or 0
    cache_write = usage.cache_write_tokens or 0
    # Per the Usage contract, prompt_tokens is the whole prompt including cache.
    plain_prompt = max((usage.prompt_tokens or 0) - cache_read - cache_write, 0)

    terms = [_term("prompt", "промпт", plain_prompt, model.prompt_price)]
    if cache_read:
        rate = model.cache_read_price or model.prompt_price
        term = _term("cache_read", "чтение кэша", cache_read, rate)
        # Charging a cache hit at the full prompt rate overstates it several
        # times over; say the number is a guess rather than let it look exact.
        term["assumed"] = model.cache_read_price == 0.0
        terms.append(term)
    if cache_write:
        rate = model.cache_write_price or model.prompt_price
        term = _term("cache_write", "запись в кэш", cache_write, rate)
        term["assumed"] = model.cache_write_price == 0.0
        terms.append(term)
    terms.append(
        _term("completion", "ответ", usage.completion_tokens, model.completion_price)
    )
    return terms


def _consumed_anything(modality: Modality, usage: Usage) -> bool:
    """Did this request use something a rate would have applied to?

    A request that consumed nothing is legitimately free; one that consumed
    something the registry cannot price is `unpriced`.
    """
    return bool(_units_for_modality(modality, usage))


def _units_for_modality(modality: Modality, usage: Usage) -> float:
    if modality is Modality.CHAT:
        units = float(
            usage.total_tokens
            or (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
        )
    elif modality is Modality.IMAGE:
        units = float(usage.images or 1)
    elif modality is Modality.TTS:
        units = float(usage.characters or usage.audio_seconds or 0)
    elif modality is Modality.STT:
        units = float(usage.audio_seconds or 0)
    elif modality in (Modality.AUDIO, Modality.VIDEO):
        # These groups have no unit of their own; compute time is what was bought.
        units = float(usage.compute_seconds or 0)
    else:
        units = float(usage.total_tokens or 1)
    # A run that reported nothing but compute time still consumed something. Without
    # this, a video run with no rate would come back as a confident zero instead of
    # `unpriced` — the exact failure the unpriced state exists to prevent.
    return units or float(usage.compute_seconds or 0)


# What each billing group is metered in, and the rate that prices it. One table
# instead of the four-way dict `_estimate` used to carry, so a new group cannot be
# added without an answer here.
@dataclass(frozen=True, slots=True)
class BillingUnitSpec:
    # The whole Russian phrase, not a bare noun: "за одну секунду" and "за одно
    # изображение" do not come out of one template, and building them from parts
    # means grammar logic in the router.
    per_unit: str
    rate: Callable[[RegistryModel], float]


BILLING_UNITS: dict[Modality, BillingUnitSpec] = {
    Modality.IMAGE: BillingUnitSpec("за одно изображение", lambda m: m.image_price),
    Modality.STT: BillingUnitSpec("за одну секунду записи", lambda m: m.audio_price_per_second),
    Modality.TTS: BillingUnitSpec(
        "за один символ", lambda m: m.character_price or m.audio_price_per_second
    ),
    Modality.AUDIO: BillingUnitSpec(
        "за одну секунду расчёта", lambda m: m.compute_price_per_second
    ),
    Modality.VIDEO: BillingUnitSpec(
        "за одну секунду расчёта", lambda m: m.compute_price_per_second
    ),
    # Token-billed groups have two rates and are estimated from the prompt length
    # instead, so they carry no single per-unit figure.
    Modality.CHAT: BillingUnitSpec("за один токен промпта", lambda m: m.prompt_price),
    Modality.CLI: BillingUnitSpec("за один токен промпта", lambda m: m.prompt_price),
}

# The groups whose cost is a rate times a unit nobody can count before the run.
# For these the estimate names the rate; for chat and cli it measures the prompt.
PER_UNIT_GROUPS = (
    Modality.IMAGE,
    Modality.STT,
    Modality.TTS,
    Modality.AUDIO,
    Modality.VIDEO,
)
