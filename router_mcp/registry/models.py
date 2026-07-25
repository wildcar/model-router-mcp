"""RegistryModel: a single row of the model registry.

Carries the three-level mapping (service_provider -> model_provider -> model_id)
plus pricing, modality and tier metadata. The cost-bucket / tier logic is lifted
from ort_bot/services/model_cache.py:CachedModel so tiering stays consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from router_mcp.domain.modality import Modality

# Per-1K-token threshold below which a text model counts as "cheap".
CHEAP_THRESHOLD_PER_1K = 0.001


@dataclass(slots=True)
class RegistryModel:
    service_provider_id: str
    model_provider: str
    model_id: str
    display_name: str
    modalities: tuple[Modality, ...]
    capabilities: tuple[str, ...] = ()
    tier: str = "cheap"  # "free" | "cheap" | "expensive"
    prompt_price: float = 0.0  # USD per token
    completion_price: float = 0.0  # USD per token
    image_price: float = 0.0  # USD per image
    audio_price_per_second: float = 0.0  # USD per second
    context_length: int = 0
    source: str = "manual"  # "catalog" | "seed" | "manual"
    is_active: bool = True
    pricing_mode: str = "per_request"  # "per_request" | "amortized_subscription"
    subscription_id: int | None = None
    # Catalog freshness. `last_seen_at` is the last sync that found the model
    # upstream; `deprecated_at` is set when a sync stopped finding it. Retired
    # rows stay in the table because request logs reference them.
    last_seen_at: str | None = None
    deprecated_at: str | None = None
    replaced_by: str | None = None
    # Price provenance: where the number came from and when it was last checked.
    # "api"   — read from the provider's own catalog, trustworthy;
    # "page"  — copied from the vendor's price page (URL in price_source);
    # "manual"— entered by an admin;
    # "estimate" — a guess nobody has confirmed (default, flagged in costs);
    # "unknown"  — the catalog lists the model but publishes no price.
    price_source: str | None = None
    price_source_kind: str = "estimate"
    price_checked_at: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def price_is_verified(self) -> bool:
        return self.price_source_kind in ("api", "page", "manual")

    @property
    def price_is_unknown(self) -> bool:
        """True when the zeros in this row mean "nobody filled the price in".

        A catalog that lists ids without prices (DeepSeek, Anthropic) leaves the
        columns at zero, which would otherwise make the model look like the
        cheapest thing in the registry and win every automatic selection.
        """
        return self.price_source_kind == "unknown" and (
            self.prompt_price == 0.0
            and self.completion_price == 0.0
            and self.image_price == 0.0
            and self.audio_price_per_second == 0.0
        )

    @property
    def is_free(self) -> bool:
        return (
            self.prompt_price == 0.0
            and self.completion_price == 0.0
            and self.image_price == 0.0
            and self.audio_price_per_second == 0.0
        )

    @property
    def is_cheap(self) -> bool:
        return ((self.prompt_price + self.completion_price) * 1000.0) < CHEAP_THRESHOLD_PER_1K

    @classmethod
    def compute_tier(cls, prompt_price: float, completion_price: float) -> str:
        if prompt_price == 0.0 and completion_price == 0.0:
            return "free"
        if ((prompt_price + completion_price) * 1000.0) < CHEAP_THRESHOLD_PER_1K:
            return "cheap"
        return "expensive"

    def supports(self, modality: Modality) -> bool:
        return modality in self.modalities
