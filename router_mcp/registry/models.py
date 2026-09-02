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

import dataclasses
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from router_mcp.domain.modality import Modality
from router_mcp.domain.task import DEFAULT_TASK, Task

# Per-1K-token threshold below which a text model counts as "cheap".
CHEAP_THRESHOLD_PER_1K = 0.001

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _minutes(text: str) -> int:
    match = _HHMM.fullmatch(str(text).strip())
    if match is None:
        raise ValueError(f"Not a UTC time of day: {text!r}; expected HH:MM")
    return int(match.group(1)) * 60 + int(match.group(2))


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True, slots=True)
class PriceSchedule:
    """Times of day at which every rate on the row is multiplied.

    DeepSeek bills peak hours at twice the off-peak rate (01:00–04:00 and
    06:00–10:00 UTC as of 2026-08-16), and the registry holds one number per
    rate. Storing the peak figure overcharges the seventeen off-peak hours;
    storing the off-peak figure undercharges the other seven. Neither is a price
    the provider actually charges, so the row carries the base rate plus the
    windows in which it doubles, and accounting multiplies at the moment of the
    run.

    A multiplier rather than a second set of columns because that is the shape of
    the discount every provider that does this has published: one schedule, all
    rates scaled together. Nine more price columns would have to be filled in
    twice and could drift apart.

    Windows are minutes of the **UTC** day, half-open `[start, end)`. A window
    whose start is after its end wraps past midnight — DeepSeek's previous
    schedule ran 16:30–00:30 UTC, so this is not a hypothetical.
    """

    windows: tuple[tuple[int, int], ...] = ()
    multiplier: float = 1.0
    # What to call the multiplied rate on screen. The registry has no vocabulary
    # of its own for "peak", and a provider may well name its window something
    # else (a night discount is the same mechanism upside down).
    label: str = "пиковый тариф"

    @property
    def is_active(self) -> bool:
        return bool(self.windows) and self.multiplier != 1.0

    def factor_at(self, moment: datetime) -> float:
        """The multiplier in force at `moment`, which is 1.0 outside every window."""
        if not self.is_active:
            return 1.0
        utc = moment.astimezone(timezone.utc)
        minute = utc.hour * 60 + utc.minute
        for start, end in self.windows:
            inside = start <= minute < end if start < end else (minute >= start or minute < end)
            if inside:
                return self.multiplier
        return 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "windows": [f"{_hhmm(start)}-{_hhmm(end)}" for start, end in self.windows],
            "multiplier": self.multiplier,
            "label": self.label,
            # Written out rather than assumed: the column is hand-edited, and a
            # reader who has to guess the zone reads the whole schedule wrong.
            "tz": "UTC",
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def parse(cls, raw: Any) -> "PriceSchedule | None":
        """A schedule from its stored JSON, or None for anything unusable.

        Strict about the times — an operator who types `25:00` wants to hear
        about it — but a row whose column holds junk loads without a schedule
        rather than taking the registry down, the same bargain `input_map` makes.
        """
        if not raw:
            return None
        data = raw
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        windows: list[tuple[int, int]] = []
        for item in data.get("windows") or ():
            if isinstance(item, str):
                parts = item.split("-")
            elif isinstance(item, (list, tuple)):
                parts = list(item)
            else:
                raise ValueError(f"Not a time window: {item!r}")
            if len(parts) != 2:
                raise ValueError(f"Not a time window: {item!r}; expected HH:MM-HH:MM")
            start, end = _minutes(parts[0]), _minutes(parts[1])
            if start == end:
                raise ValueError(f"Empty time window: {item!r}")
            windows.append((start, end))
        try:
            multiplier = float(data.get("multiplier", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Not a multiplier: {data.get('multiplier')!r}") from exc
        if multiplier <= 0:
            raise ValueError("A price multiplier must be positive")
        label = str(data.get("label") or "пиковый тариф")
        if not windows or multiplier == 1.0:
            return None
        return cls(tuple(windows), multiplier, label)


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
    # Server-side web search is billed per search call, on top of the tokens
    # (OpenRouter publishes it as `pricing.web_search`; gpt-5.4-mini ran two
    # searches in one request and each cost this much). Zero means "nobody
    # filled this in", and accounting then marks a searched run as an estimate
    # rather than quietly understating it.
    web_search_price: float = 0.0  # USD per web search call
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
    # Times of day at which the rates above are multiplied (DeepSeek's peak
    # hours). None means the row costs the same round the clock, which is every
    # other row here. Human-written like `input_map`: no catalog publishes it.
    price_schedule: PriceSchedule | None = None
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
    # Attachment role -> this model's own input property, e.g.
    # `{"image": "human_img", "garment": "garm_img"}` for a try-on model. Only a
    # human can write it: the schema publishes `human_img` and `garm_img` and
    # says nothing about which is which, so an adapter that guesses gets it right
    # half the time. Empty means "bind attachments positionally", which is what
    # every single-image model wants and what the router did before roles.
    input_map: dict[str, str] = field(default_factory=dict)
    # Parameter names this model accepts on top of the adapter's declared set,
    # e.g. `("scale",)` for an upscaler or `("category",)` for a try-on model.
    # An allowlist, per model, written by a human: adding an upscaler used to
    # mean editing `SUPPORTED_PARAMS` in the adapter, so an operator could add
    # the model through the console but not reach its own knobs. It is still not
    # a passthrough — a name the model's schema has no property for is dropped
    # and reported, exactly like a declared one.
    extra_params: tuple[str, ...] = ()
    # What this model does and, more usefully, does not: "one sheet in frame,
    # edges visible; a two-page spread is unpredictable". Free text for the
    # console and for the applications that let a user pick a model. Written
    # by a human (or a seed), never by a catalog sync — no provider publishes
    # the caveats.
    description: str = ""
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

    # The rates a schedule scales. All of them: a provider that raises its prices
    # for eight hours raises the price of the row, and a column left out here
    # would quietly bill the off-peak figure during the peak.
    _SCHEDULED_RATES = (
        "prompt_price",
        "completion_price",
        "image_price",
        "audio_price_per_second",
        "character_price",
        "cache_read_price",
        "cache_write_price",
        "web_search_price",
        "compute_price_per_second",
    )

    def priced_at(self, moment: datetime | None = None) -> "RegistryModel":
        """This row with the rates in force at `moment` (default: now, UTC).

        Returns `self` when nothing scales, so the common row — every row but
        DeepSeek's two — allocates nothing and compares identical. Everything
        downstream (`compute_cost`, `billing_unit`, `ladder_price`, the dry-run
        estimate) then works on ordinary rates and needs to know nothing about
        schedules.
        """
        schedule = self.price_schedule
        if schedule is None or not schedule.is_active:
            return self
        factor = schedule.factor_at(moment or datetime.now(timezone.utc))
        if factor == 1.0:
            return self
        scaled = {name: getattr(self, name) * factor for name in self._SCHEDULED_RATES}
        return dataclasses.replace(self, **scaled)

    def price_factor_at(self, moment: datetime | None = None) -> float:
        """What the rates on this row are multiplied by at `moment`."""
        if self.price_schedule is None:
            return 1.0
        return self.price_schedule.factor_at(moment or datetime.now(timezone.utc))

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
