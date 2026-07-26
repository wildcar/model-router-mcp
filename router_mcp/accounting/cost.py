"""Per-request cost computation.

Precedence:
  1. provider-reported cost (e.g. claude CLI total_cost_usd) -> used verbatim
  2. amortized subscription (hook) -> monthly_cost / monthly_quota_units * units
  3. per-request -> from registry prices x usage, by modality

Prices follow OpenRouter conventions: USD per token (chat), USD per image,
USD per second (audio), USD per character (TTS). Arithmetic uses Decimal to
avoid float drift.

Two rules the rest of the system leans on (§7 of docs/UI_CONCEPT_v2.md):

* every number comes back with a `cost_source` saying how much to trust it, and
  "нет методики" is one of those states rather than a zero;
* the sum arrives with its terms, each carrying its own rate, so a screen can
  print the arithmetic instead of a total the operator has to believe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

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
        not model.price_is_verified
        and not model.has_rate_for(modality)
        and _consumed_anything(modality, usage)
    ):
        # The request used something and nobody ever established a rate for it.
        # Reporting 0.0 here is what made image, stt and tts bill at exactly
        # nothing. A zero somebody *confirmed* is a different thing and falls
        # through to the arithmetic below, which correctly yields zero.
        return CostResult(0.0, "unpriced", "per_request")

    terms = _terms(model, modality, usage)
    total = sum((_d(t["amount"]) for t in terms), Decimal(0))
    # A price with no confirmed origin yields a number that only looks precise,
    # and so does a cache rate we had to guess at.
    guessed_cache = any(t.get("assumed") for t in terms)
    source = "computed" if model.price_is_verified and not guessed_cache else "estimated"
    return CostResult(float(total), source, "per_request", terms)


def _terms(model: RegistryModel, modality: Modality, usage: Usage) -> list[dict[str, Any]]:
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
        return float(usage.total_tokens or (usage.prompt_tokens or 0) + (usage.completion_tokens or 0))
    if modality is Modality.IMAGE:
        return float(usage.images or 1)
    if modality is Modality.TTS:
        return float(usage.characters or usage.audio_seconds or 0)
    if modality is Modality.STT:
        return float(usage.audio_seconds or 0)
    return float(usage.total_tokens or 1)
