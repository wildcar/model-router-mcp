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
    source: str = "manual"  # "catalog" | "manual"
    is_active: bool = True
    pricing_mode: str = "per_request"  # "per_request" | "amortized_subscription"
    subscription_id: int | None = None
    extra: dict = field(default_factory=dict)

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
