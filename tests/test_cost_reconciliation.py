"""The reconciliation filter: our arithmetic against the provider's charge.

Both real pricing gaps so far (the unrecorded web-search fee, the unparsed
cache rates) surfaced as one symptom — `cost_usd` sitting away from the sum the
provider itself reported. These tests pin the filter that turns that symptom
into a work-list line instead of a manual check.
"""

from __future__ import annotations

import pytest

from router_mcp.storage.database import Database, RequestLogRecord


def _rec(cost: float, provider: float | None, source: str = "computed", **kw) -> RequestLogRecord:
    return RequestLogRecord(
        external_user_id="u",
        modality="chat",
        cost_usd=cost,
        cost_source=source,
        provider_cost_usd=provider,
        **kw,
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "recon.db"))
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_only_a_real_divergence_is_flagged(db):
    await db.log_request(_rec(0.011, 0.028))          # the search-fee shape: flag
    await db.log_request(_rec(0.0149269, 0.0149269))  # exact match: quiet
    await db.log_request(_rec(0.0100, 0.0101))        # 1% apart: inside tolerance
    await db.log_request(_rec(0.02, None))            # provider said nothing
    rows = await db.list_request_logs(cost_mismatch=True)
    assert [r["cost_usd"] for r in rows] == [0.011]
    assert await db.count_request_logs(cost_mismatch=True) == 1


@pytest.mark.asyncio
async def test_float_dust_stays_out_of_the_work_list(db):
    # Sub-hundredth-of-a-cent noise on a micro-request is storage, not pricing.
    await db.log_request(_rec(0.00010000, 0.00010002))
    assert await db.count_request_logs(cost_mismatch=True) == 0


@pytest.mark.asyncio
async def test_a_systematic_miss_on_a_cheap_request_still_flags(db):
    # Twice the money on a $0.001 run must not hide under an absolute floor.
    await db.log_request(_rec(0.001, 0.002))
    assert await db.count_request_logs(cost_mismatch=True) == 1


@pytest.mark.asyncio
async def test_sources_equal_by_construction_are_excluded(db):
    # For these the recorded cost *is* the provider figure (or a subscription
    # spread) — comparing would flag arithmetic that never ran.
    await db.log_request(_rec(0.05, 0.06, source="provider"))
    await db.log_request(_rec(0.05, 0.06, source="provider_notional"))
    await db.log_request(_rec(0.05, 0.06, source="amortized"))
    assert await db.count_request_logs(cost_mismatch=True) == 0


@pytest.mark.asyncio
async def test_an_unpriced_run_the_provider_charged_for_is_flagged(db):
    # "No methodology" plus a real charge is exactly a price worth entering.
    await db.log_request(_rec(0.0, 0.04, source="unpriced"))
    rows = await db.list_request_logs(cost_mismatch=True)
    assert len(rows) == 1 and rows[0]["provider_cost_usd"] == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_the_filter_composes_with_the_time_window(db):
    await db.log_request(_rec(0.011, 0.028))
    n = await db.count_request_logs(cost_mismatch=True, since="2020-01-01T00:00:00Z")
    assert n == 1
    n = await db.count_request_logs(cost_mismatch=True, since="2999-01-01T00:00:00Z")
    assert n == 0
