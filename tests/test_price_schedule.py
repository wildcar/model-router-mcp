"""Peak-hour pricing: one row, two rates, and the clock decides which.

DeepSeek moved to peak/off-peak billing on 2026-08-16 — 01:00–04:00 and
06:00–10:00 UTC cost twice the rest of the day. The registry holds one number per
rate, so either figure alone is wrong for most of the day: the peak price
overcharges seventeen hours, the off-peak price undercharges seven. The row
therefore carries the base rate plus the windows in which it doubles, and every
place that reads a rate reads it *at a moment*.

What these pin:

1. the schedule parses, round-trips through its JSON column and survives a
   catalog sync, because nothing upstream publishes it;
2. a run inside a window is charged double and says so, and the same run an hour
   later is not;
3. the ladder and the automatic scan compare models at the rates in force, so a
   model in its peak hours is not picked as the cheap one;
4. a broken column costs the base rate rather than taking the registry down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from router_mcp.accounting.cost import compute_cost
from router_mcp.domain.messages import Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import PriceSchedule, RegistryModel

# DeepSeek's own schedule, which is what the two live rows carry.
DEEPSEEK = {
    "windows": ["01:00-04:00", "06:00-10:00"],
    "multiplier": 2.0,
    "label": "пиковый тариф",
}


def _model(**kw) -> RegistryModel:
    defaults = dict(
        service_provider_id="deepseek", model_provider="deepseek", model_id="m",
        display_name="m", modalities=(Modality.CHAT,), price_source_kind="page",
        prompt_price=0.22 / 1e6, completion_price=0.66 / 1e6,
        cache_read_price=0.007 / 1e6,
        price_schedule=PriceSchedule.parse(DEEPSEEK),
    )
    defaults.update(kw)
    return RegistryModel(**defaults)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, tzinfo=timezone.utc)


# --- the schedule itself -------------------------------------------------
@pytest.mark.parametrize(
    "moment, factor",
    [
        (_at(0, 59), 1.0),   # just before the first window
        (_at(1, 0), 2.0),    # the window is half-open at the start: 01:00 is peak
        (_at(3, 59), 2.0),
        (_at(4, 0), 1.0),    # ...and open at the end: 04:00 is not
        (_at(5, 30), 1.0),   # the gap between the two windows
        (_at(6, 0), 2.0),
        (_at(9, 59), 2.0),
        (_at(10, 0), 1.0),
        (_at(16, 0), 1.0),
    ],
)
def test_the_factor_follows_the_clock(moment, factor):
    assert _model().price_factor_at(moment) == factor


def test_a_window_may_wrap_past_midnight():
    """DeepSeek's previous schedule ran 16:30–00:30 UTC, so this is not academic."""
    night = PriceSchedule.parse({"windows": ["16:30-00:30"], "multiplier": 0.5})
    assert night.factor_at(_at(23, 0)) == 0.5
    assert night.factor_at(_at(0, 15)) == 0.5
    assert night.factor_at(_at(16, 29)) == 1.0
    assert night.factor_at(_at(12, 0)) == 1.0


def test_the_factor_is_read_in_utc_whatever_the_caller_carries():
    """A run stamped in +05:00 is priced by the UTC hour, not the local one."""
    local = datetime(2026, 8, 17, 6, 0, tzinfo=timezone(timedelta(hours=5)))  # 01:00 UTC
    assert _model().price_factor_at(local) == 2.0


def test_a_schedule_that_multiplies_by_one_is_no_schedule():
    """Windows with a multiplier of 1 describe nothing, and a row that carries
    them would take the copying path in `priced_at` for no reason."""
    assert PriceSchedule.parse({"windows": ["01:00-04:00"], "multiplier": 1.0}) is None
    assert PriceSchedule.parse({"windows": [], "multiplier": 2.0}) is None
    assert PriceSchedule.parse("") is None


def test_nonsense_times_are_refused_rather_than_rounded():
    for bad in ({"windows": ["25:00-26:00"], "multiplier": 2.0},
                {"windows": ["01:00"], "multiplier": 2.0},
                {"windows": ["01:00-01:00"], "multiplier": 2.0},
                {"windows": ["01:00-04:00"], "multiplier": -1}):
        with pytest.raises(ValueError):
            PriceSchedule.parse(bad)


def test_the_schedule_round_trips_through_its_column():
    schedule = PriceSchedule.parse(DEEPSEEK)
    assert PriceSchedule.parse(schedule.to_json()) == schedule
    assert schedule.to_dict()["windows"] == ["01:00-04:00", "06:00-10:00"]
    # The zone is written out rather than assumed: whoever reads the column next
    # must not have to guess which clock the hours are on.
    assert schedule.to_dict()["tz"] == "UTC"


def test_a_flat_row_is_returned_unchanged():
    """Every row but DeepSeek's two, so it must cost nothing to ask."""
    flat = _model(price_schedule=None)
    assert flat.priced_at(_at(2)) is flat
    assert flat.price_factor_at(_at(2)) == 1.0


def test_every_rate_scales_together():
    """A column left out of `_SCHEDULED_RATES` would bill the off-peak figure
    during the peak, which is the whole defect this exists to prevent."""
    row = _model(image_price=0.04, character_price=1e-5, compute_price_per_second=0.001,
                 audio_price_per_second=0.002, web_search_price=0.01,
                 cache_write_price=1e-7)
    peak = row.priced_at(_at(2))
    for name in RegistryModel._SCHEDULED_RATES:
        assert getattr(peak, name) == pytest.approx(getattr(row, name) * 2.0), name


# --- what a run is charged -----------------------------------------------
def test_a_run_in_the_peak_is_charged_double_and_says_why():
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    peak = compute_cost(_model(), Modality.CHAT, usage, at=_at(2))
    off = compute_cost(_model(), Modality.CHAT, usage, at=_at(12))

    assert off.cost_usd == pytest.approx(0.88)   # 0.22 + 0.66
    assert peak.cost_usd == pytest.approx(1.76)  # ...and twice that in the peak
    # The rates in the breakdown are the multiplied ones, so the arithmetic on
    # screen adds up. Which means the factor has to travel with them, or an
    # operator comparing the log against the price table sees two numbers that
    # disagree and no reason why.
    assert peak.price_factor == 2.0
    assert peak.price_factor_label == "пиковый тариф"
    assert off.price_factor == 1.0
    prompt_term = next(t for t in peak.breakdown if t["kind"] == "prompt")
    assert prompt_term["rate"] == pytest.approx(0.44 / 1e6)
    # Still a computed number, not an estimate: the price is confirmed and so is
    # the schedule multiplying it.
    assert peak.cost_source == "computed"


def test_the_cache_rate_scales_with_the_rest():
    usage = Usage(prompt_tokens=1_000_000, cache_read_tokens=1_000_000, completion_tokens=0)
    peak = compute_cost(_model(), Modality.CHAT, usage, at=_at(7))
    assert peak.cost_usd == pytest.approx(0.014)  # 0.007 off-peak, doubled
    term = next(t for t in peak.breakdown if t["kind"] == "cache_read")
    assert term["assumed"] is False


def test_a_provider_reported_sum_is_not_multiplied():
    """The provider already charged what it charged; a peak multiplier on top of
    its own figure would invent money."""
    result = compute_cost(_model(), Modality.CHAT, Usage(), override_usd=0.041, at=_at(2))
    assert result.cost_usd == 0.041
    assert result.price_factor == 1.0


def test_an_unpriced_row_stays_unpriced_in_the_peak():
    """Twice nothing is still nothing, and it must not start looking like a price."""
    empty = _model(prompt_price=0.0, completion_price=0.0, cache_read_price=0.0,
                   price_source_kind="unknown")
    result = compute_cost(empty, Modality.CHAT, Usage(prompt_tokens=10), at=_at(2))
    assert result.cost_source == "unpriced"
    assert result.cost_usd == 0.0


# --- selection -----------------------------------------------------------
def test_the_ladder_compares_the_rates_in_force():
    """Ordering on the base rate would call a row in its peak hours the cheap one
    while it is billing double."""
    scheduled = _model()
    rival = _model(model_id="rival", price_schedule=None,
                   prompt_price=0.30 / 1e6, completion_price=0.90 / 1e6)
    off, peak = _at(12), _at(2)
    assert scheduled.priced_at(off).ladder_price(Modality.CHAT) < \
        rival.priced_at(off).ladder_price(Modality.CHAT)
    assert scheduled.priced_at(peak).ladder_price(Modality.CHAT) > \
        rival.priced_at(peak).ladder_price(Modality.CHAT)


# --- the column ----------------------------------------------------------
@pytest.mark.asyncio
async def test_the_schedule_survives_a_catalog_sync(tmp_path):
    """DeepSeek's catalog lists ids and no prices at all, so a sync that wrote
    this column would erase the only copy of the schedule there is."""
    from router_mcp.storage.database import Database

    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.upsert_service_provider("deepseek", "DeepSeek", "deepseek", base_url=None)
    row = RegistryModel(
        service_provider_id="deepseek", model_provider="deepseek",
        model_id="deepseek-v4-flash", display_name="flash",
        modalities=(Modality.CHAT,), source="catalog",
    )
    await db.upsert_registry_model(row)
    await db.update_registry_model(
        "deepseek", "deepseek-v4-flash",
        {"price_schedule": PriceSchedule.parse(DEEPSEEK).to_json()},
    )

    # A second sync of the same catalog row: prices, names and dates are the
    # catalog's to write, the schedule is not.
    await db.upsert_registry_model(row)
    stored = await db.get_registry_model("deepseek", "deepseek-v4-flash")
    assert stored.price_schedule == PriceSchedule.parse(DEEPSEEK)
    await db.close()


@pytest.mark.asyncio
async def test_a_broken_column_costs_the_base_rate_rather_than_failing(tmp_path):
    """Hand-edited text is the one thing here that can be malformed at rest.
    Billing flat for a few hours is wrong by a factor of two; a registry that
    will not load is wrong for everything."""
    from router_mcp.storage.database import Database

    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.upsert_service_provider("deepseek", "DeepSeek", "deepseek", base_url=None)
    await db.upsert_registry_model(
        RegistryModel(
            service_provider_id="deepseek", model_provider="deepseek",
            model_id="m", display_name="m", modalities=(Modality.CHAT,),
        )
    )
    conn = await db._get_conn()
    await conn.execute("UPDATE registry_models SET price_schedule = '{oops'")
    await conn.commit()

    stored = await db.get_registry_model("deepseek", "m")
    assert stored.price_schedule is None
    assert stored.price_factor_at(_at(2)) == 1.0
    await db.close()
