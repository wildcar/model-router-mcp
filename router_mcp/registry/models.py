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
    # TTS is billed per character, not per second of the audio it produces:
    # pricing a synthesis run by its duration prices the speaking rate.
    character_price: float = 0.0  # USD per character
    # Cached prompt tokens are billed at their own rate — roughly a tenth of the
    # prompt rate at DeepSeek and Anthropic. Zero means "nobody filled this in",
    # and accounting then falls back to the prompt rate and says the number is
    # an estimate rather than quietly making the request look cheap.
    cache_read_price: float = 0.0  # USD per cached prompt token
    cache_write_price: float = 0.0  # USD per token written to cache
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
    # Last write to the row, both set by the database. `revision` is what the
    # console sends back on an edit: a timestamp has second resolution, and two
    # writes inside one second would slip through the optimistic lock.
    updated_at: str | None = None
    revision: int = 0
    extra: dict = field(default_factory=dict)

    # Provenances under which a zero price means "confirmed free" rather than
    # "not filled in". A catalog that publishes prices, or an administrator, can
    # say a model costs nothing; a guess cannot.
    _CONFIRMED_PROVENANCE = ("api", "page", "manual")

    @property
    def price_is_verified(self) -> bool:
        return self.price_source_kind in self._CONFIRMED_PROVENANCE

    @property
    def price_is_unknown(self) -> bool:
        """True when the zeros in this row mean "nobody filled the price in".

        Two ways a row ends up with zeros nobody chose: a catalog that lists ids
        without prices (DeepSeek, Anthropic) stores them as `unknown`, and a
        bootstrap seed for a provider with no catalog at all (image, stt, tts)
        stores them as `estimate`. Both used to read as "free", which made them
        the cheapest thing in the registry and won every automatic selection —
        and made three of the five modalities bill at exactly zero.

        A zero that somebody confirmed is a different thing and stays free.
        """
        return not self.price_is_verified and self._all_prices_zero

    @property
    def _all_prices_zero(self) -> bool:
        return (
            self.prompt_price == 0.0
            and self.completion_price == 0.0
            and self.image_price == 0.0
            and self.audio_price_per_second == 0.0
            and self.character_price == 0.0
        )

    @property
    def is_free(self) -> bool:
        """Free as a fact, not as a gap: confirmed provenance and zero rates."""
        return self._all_prices_zero and self.price_is_verified

    def has_rate_for(self, modality: Modality) -> bool:
        """Whether this row can price that modality at all.

        `unpriced` on a run means exactly this returning False while the run
        consumed something: there is no methodology, and a zero would be a
        statement rather than a gap (§2, §7.2).
        """
        if modality is Modality.IMAGE:
            return self.image_price > 0.0
        if modality is Modality.TTS:
            return self.character_price > 0.0 or self.audio_price_per_second > 0.0
        if modality is Modality.STT:
            return self.audio_price_per_second > 0.0
        # chat and cli are token-billed; either rate being set is enough.
        return self.prompt_price > 0.0 or self.completion_price > 0.0

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
