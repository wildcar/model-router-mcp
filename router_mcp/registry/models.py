"""RegistryModel: a single row of the model registry.

Carries the three-level mapping (service_provider -> model_provider -> model_id)
plus pricing, task and tier metadata. The cost-bucket / tier logic is lifted
from ort_bot/services/model_cache.py:CachedModel so tiering stays consistent.

`tasks` is what routing matches on (ADR-0006); `modalities` is the billing group
each of those tasks falls into, kept because the prices below are per group and
because `registry_models.modality` still stores it. The two are derived from each
other, so a row built either way loads.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from router_mcp.domain.modality import Modality
from router_mcp.domain.task import DEFAULT_TASK, Task

# Per-1K-token threshold below which a text model counts as "cheap".
CHEAP_THRESHOLD_PER_1K = 0.001


@dataclass(slots=True)
class RegistryModel:
    service_provider_id: str
    model_provider: str
    model_id: str
    display_name: str
    # The billing groups this row can be charged in. Derived from `tasks` when
    # only tasks were given; kept as the stored form of `registry_models.modality`.
    # Keeps its position (and gains a default) because callers construct this row
    # positionally, and moving it would silently reassign their arguments.
    modalities: tuple[Modality, ...] = ()
    # What this model can actually be asked to do. This is the routing key, and
    # the axis a caller should prefer.
    tasks: tuple[Task, ...] = ()
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
    # Seconds of *provider compute*, which is not seconds of audio: Replicate
    # bills a hosted model by how long its hardware ran, and that quantity has
    # nothing to do with the length of the file it produced. Two different
    # seconds in one column would make every spend report unreadable, so this
    # rate is its own. Metered from the provider's own figure
    # (`Usage.compute_seconds`), never from our wall clock.
    compute_price_per_second: float = 0.0  # USD per second of provider compute
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
    # When a request to this exact pair last came back with an answer. This is
    # the only proof a model id is real: a catalog entry says the provider lists
    # it, a seed says somebody typed it, and both are routinely wrong. Rows the
    # registry keeps for good are the confirmed ones plus what an admin curated;
    # everything else is a candidate that a sweep may retire (ADR-0008).
    confirmed_at: str | None = None
    extra: dict = field(default_factory=dict)

    # Provenances under which a zero price means "confirmed free" rather than
    # "not filled in". A catalog that publishes prices, or an administrator, can
    # say a model costs nothing; a guess cannot.
    _CONFIRMED_PROVENANCE = ("api", "page", "manual")

    def __post_init__(self) -> None:
        """Fill whichever axis the caller did not give.

        A catalog adapter knows the tasks; a hand-written seed and every row
        written before ADR-0006 know only the group. Deriving both ways keeps one
        constructor for both, and keeps a row written by the previous build
        loadable.
        """
        if self.tasks and not self.modalities:
            groups: list[Modality] = []
            for task in self.tasks:
                if task.billing_group not in groups:
                    groups.append(task.billing_group)
            self.modalities = tuple(groups)
        elif self.modalities and not self.tasks:
            self.tasks = tuple(
                DEFAULT_TASK[group] for group in self.modalities if group in DEFAULT_TASK
            )
        elif not self.modalities and not self.tasks:
            # A row that declares neither can never be selected for anything, and
            # the silence is worse than the error: it just never appears as a
            # candidate. Both axes are optional individually, not together.
            raise ValueError(
                f"Model {self.service_provider_id}/{self.model_id} declares "
                "neither tasks nor modalities"
            )

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
        and made three of the five billing groups bill at exactly zero.

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
            # Without this a model whose only rate is per second of compute would
            # read as "price unknown" forever, and stay last in every selection
            # even after an operator had priced it.
            and self.compute_price_per_second == 0.0
        )

    @property
    def is_free(self) -> bool:
        """Free as a fact, not as a gap: confirmed provenance and zero rates."""
        return self._all_prices_zero and self.price_is_verified

    def has_unit_rate_for(self, modality: Modality) -> bool:
        """Whether a per-unit rate exists for that billing group.

        Separate from `has_rate_for` because a provider that bills per second of
        its own compute can price a run of *any* group, while a per-unit rate is
        the more specific answer and wins when both exist.
        """
        if modality is Modality.IMAGE:
            return self.image_price > 0.0
        if modality is Modality.TTS:
            return self.character_price > 0.0 or self.audio_price_per_second > 0.0
        if modality is Modality.STT:
            return self.audio_price_per_second > 0.0
        if modality in (Modality.AUDIO, Modality.VIDEO):
            # Generated sound and video have no per-unit rate of their own: the
            # providers hosting them charge for compute time.
            return False
        # chat and cli are token-billed; either rate being set is enough.
        return self.prompt_price > 0.0 or self.completion_price > 0.0

    def has_rate_for(self, modality: Modality) -> bool:
        """Whether this row can price that group at all.

        `unpriced` on a run means exactly this returning False while the run
        consumed something: there is no methodology, and a zero would be a
        statement rather than a gap (§2, §7.2).
        """
        return self.has_unit_rate_for(modality) or self.compute_price_per_second > 0.0

    def ladder_price(self, modality: Modality) -> float:
        """The rate the fallback ladder orders candidates by, for that group.

        The ladder used to sort by `prompt_price + completion_price`, which is
        zero for every image, stt, tts, audio and video row — so for most groups
        it was really sorted by `model_id`, and «дешевле» meant nothing.
        """
        if modality is Modality.IMAGE:
            rate = self.image_price
        elif modality is Modality.TTS:
            rate = self.character_price or self.audio_price_per_second
        elif modality is Modality.STT:
            rate = self.audio_price_per_second
        elif modality in (Modality.AUDIO, Modality.VIDEO):
            rate = self.compute_price_per_second
        else:
            rate = self.prompt_price + self.completion_price
        # A group with no rate of its own still has to order somehow, and compute
        # time is the only figure left. Unpriced rows never reach the ladder.
        return rate or self.compute_price_per_second

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

    def supports(self, what: Task | Modality) -> bool:
        """Whether this row can serve that task (or anything in that group).

        Accepts a `Modality` too, because the per-group defaults from settings and
        the pinned endpoints only know the group. A group match is deliberately
        weaker than a task match: it says "this row bills here", not "it can do
        this". Routing asks with a `Task`.
        """
        if isinstance(what, Task):
            return what in self.tasks
        return what in self.modalities
