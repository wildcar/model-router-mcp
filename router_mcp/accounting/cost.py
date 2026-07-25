"""Per-request cost computation.

Precedence:
  1. provider/manual override (e.g. claude CLI total_cost_usd) -> used verbatim
  2. amortized subscription (hook) -> monthly_cost / monthly_quota_units * units
  3. per-request -> from registry prices x usage, by modality

Prices follow OpenRouter conventions: USD per token (chat), USD per image,
USD per second (audio). Arithmetic uses Decimal to avoid float drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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
    # "manual" | "amortized" | "provider" — as before.
    cost_source: str
    pricing_mode: str  # "per_request" | "amortized_subscription"


def _d(value: float | int | None) -> Decimal:
    return Decimal(str(value or 0))


def compute_cost(
    model: RegistryModel,
    modality: Modality,
    usage: Usage,
    *,
    override_usd: float | None = None,
    subscription: SubscriptionRecord | None = None,
) -> CostResult:
    # 1. Provider/manual override.
    if override_usd is not None:
        return CostResult(float(override_usd), "provider", model.pricing_mode)

    # 2. Amortized subscription (design hook).
    if model.pricing_mode == "amortized_subscription" and subscription is not None:
        units = _units_for_modality(modality, usage)
        quota = subscription.monthly_quota_units
        if quota and quota > 0:
            amortized = (_d(subscription.monthly_cost) / _d(quota)) * _d(units)
            return CostResult(float(amortized), "amortized", "amortized_subscription")
        # No quota -> cannot attribute per-request; record zero but flag mode.
        return CostResult(0.0, "amortized", "amortized_subscription")

    # 3. Per-request from registry prices. A price with no confirmed origin
    # yields a number that only looks precise, so it is labelled as such.
    cost = _per_request_cost(model, modality, usage)
    source = "computed" if model.price_is_verified else "estimated"
    return CostResult(float(cost), source, "per_request")


def _per_request_cost(model: RegistryModel, modality: Modality, usage: Usage) -> Decimal:
    if modality is Modality.CHAT:
        return _d(usage.prompt_tokens) * _d(model.prompt_price) + _d(
            usage.completion_tokens
        ) * _d(model.completion_price)
    if modality is Modality.IMAGE:
        return _d(usage.images or 1) * _d(model.image_price)
    if modality in (Modality.STT, Modality.TTS):
        return _d(usage.audio_seconds) * _d(model.audio_price_per_second)
    # CLI and anything else: tokens if available, else zero.
    return _d(usage.prompt_tokens) * _d(model.prompt_price) + _d(
        usage.completion_tokens
    ) * _d(model.completion_price)


def _units_for_modality(modality: Modality, usage: Usage) -> float:
    if modality is Modality.CHAT:
        return float(usage.total_tokens or 0)
    if modality is Modality.IMAGE:
        return float(usage.images or 1)
    if modality in (Modality.STT, Modality.TTS):
        return float(usage.audio_seconds or 0)
    return float(usage.total_tokens or 1)
