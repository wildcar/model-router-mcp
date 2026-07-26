"""The run registry: progress while it runs, cancellation, idempotency.

The property under test throughout is that a run is accounted for at every
moment — before it starts, while it is going, when it is cancelled and after a
restart. A run that is invisible until it finishes is the thing wave 3 exists to
remove, and a run that vanishes on cancellation takes its cost with it.
"""

from __future__ import annotations

import asyncio

import pytest

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain import progress
from router_mcp.domain.errors import Conflict, NotFound, ProviderError
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database
from router_mcp.web.runs import RunRegistry


class SlowAdapter(ProviderAdapter):
    """Reports two steps, then waits to be released (or cancelled)."""

    name = "vendor"
    supported_tasks = {Task.TEXT_TO_TEXT}

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = False
        self.fail = fail

    def is_configured(self) -> bool:
        return True

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        progress.emit("cli", "первый шаг")
        self.started.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.fail:
            raise self.fail
        progress.emit("cli", "второй шаг")
        return RouterResponse(
            task=Task.TEXT_TO_TEXT,
            provider=self.name,
            model_id=model.model_id,
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            text="готово",
        )


class FakeCore:
    def __init__(self, db: Database, router: ModelRouter) -> None:
        self.db = db
        self.router = router


def _model() -> RegistryModel:
    return RegistryModel(
        service_provider_id="vendor",
        model_provider="vendor",
        model_id="m1",
        display_name="m1",
        modalities=(Modality.CHAT,),
        tier="cheap",
        prompt_price=1e-6,
        completion_price=1e-6,
        source="manual",
        price_source_kind="manual",
    )


async def _setup(tmp_path, adapter: SlowAdapter):
    db = Database(str(tmp_path / "runs.db"))
    await db.init()
    await db.upsert_service_provider("vendor", "Vendor", "vendor", None, enabled=True)
    await db.upsert_registry_model(_model())
    registry = RegistryService(db, adapters={})
    router = ModelRouter(db, registry, {"vendor": adapter}, BlobStore(str(tmp_path / "b")))
    return db, RunRegistry(FakeCore(db, router))


def _request(**kwargs) -> RouterRequest:
    return RouterRequest(
        task=Task.TEXT_TO_TEXT,
        external_user_id="u",
        model_id="m1",
        text="привет",
        channel="ui",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_a_run_is_visible_in_the_log_before_it_finishes(tmp_path):
    """The row is written when the run starts, not when it returns.

    Without it a five-minute CLI run is absent from the log for five minutes,
    and a second tab has no way to learn it is happening at all.
    """
    adapter = SlowAdapter()
    db, runs = await _setup(tmp_path, adapter)
    try:
        run = await runs.submit(_request(), label="m1")
        await asyncio.wait_for(adapter.started.wait(), timeout=2)

        rows = await db.list_request_logs(external_user_id="u")
        assert [r["status"] for r in rows] == ["running"]
        assert runs.get(run.id).status == "running"
        # The live timeline is already filling up while the adapter works.
        assert any(step.get("title") == "первый шаг" for step in run.steps)

        adapter.gate.set()
        await asyncio.wait_for(run.runner, timeout=2)
        assert run.status == "ok"
        rows = await db.list_request_logs(external_user_id="u")
        assert [r["status"] for r in rows] == ["ok"]
        # One run, one row: opening the row updated it rather than adding a second.
        assert rows[0]["id"] == run.result["log_id"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancelling_stops_the_run_and_closes_the_row(tmp_path):
    """A cancelled run is recorded as cancelled with the cost unknown.

    The provider may already have done the work, so recording a confident zero
    would understate the bill exactly when nobody is watching.
    """
    adapter = SlowAdapter()
    db, runs = await _setup(tmp_path, adapter)
    try:
        run = await runs.submit(_request(), label="m1")
        await asyncio.wait_for(adapter.started.wait(), timeout=2)
        await runs.cancel(run.id)

        assert adapter.cancelled is True
        assert runs.get(run.id).status == "cancelled"
        rows = await db.list_request_logs(external_user_id="u")
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["cost_source"] == "cost_unknown"

        # Cancelling twice is a conflict, not a second cancellation.
        with pytest.raises(Conflict):
            await runs.cancel(run.id)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_same_idempotency_key_does_not_run_twice(tmp_path):
    """A double-submitted form is one run, not two charges."""
    adapter = SlowAdapter()
    db, runs = await _setup(tmp_path, adapter)
    try:
        first = await runs.submit(_request(client_request_id="k-1"), label="m1")
        await asyncio.wait_for(adapter.started.wait(), timeout=2)
        again = await runs.submit(_request(client_request_id="k-1"), label="m1")
        assert again.id == first.id

        adapter.gate.set()
        await asyncio.wait_for(first.runner, timeout=2)

        # And once it is finished the key still returns the stored result, even
        # after the registry has forgotten the run (a restart, an eviction).
        runs._runs.clear()
        runs._by_key.clear()
        replayed = await runs.submit(_request(client_request_id="k-1"), label="m1")
        assert replayed.status == "ok"
        assert replayed.result["log_id"] == first.result["log_id"]
        assert len(await db.list_request_logs(external_user_id="u")) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_failed_run_closes_its_own_row(tmp_path):
    adapter = SlowAdapter(fail=ProviderError("провайдер отказал"))
    db, runs = await _setup(tmp_path, adapter)
    try:
        run = await runs.submit(_request(), label="m1")
        await asyncio.wait_for(adapter.started.wait(), timeout=2)
        adapter.gate.set()
        await asyncio.wait_for(run.runner, timeout=2)

        assert run.status == "error"
        assert run.error["error"]["code"] == "provider_error"
        rows = await db.list_request_logs(external_user_id="u")
        assert len(rows) == 1
        assert rows[0]["status"] == "error"
        assert run.error["error"]["log_id"] == rows[0]["id"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_restart_marks_live_rows_interrupted(tmp_path):
    """Nothing outside the process that started a run can finish it.

    Left alone those rows read «выполняется» forever, and the console cannot
    tell them from a call that is genuinely in flight.
    """
    adapter = SlowAdapter()
    db, runs = await _setup(tmp_path, adapter)
    try:
        run = await runs.submit(_request(), label="m1")
        await asyncio.wait_for(adapter.started.wait(), timeout=2)

        # What build_core does on the next start.
        assert await db.interrupt_live_logs() == 1
        rows = await db.list_request_logs(external_user_id="u")
        assert rows[0]["status"] == "interrupted"
        assert rows[0]["cost_source"] == "cost_unknown"

        run.runner.cancel()
        await asyncio.gather(run.runner, return_exceptions=True)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_timeline_survives_in_the_log(tmp_path):
    """Opening the row a week later replays the steps the operator watched."""
    adapter = SlowAdapter()
    db, runs = await _setup(tmp_path, adapter)
    try:
        run = await runs.submit(_request(), label="m1")
        await asyncio.wait_for(adapter.started.wait(), timeout=2)
        adapter.gate.set()
        await asyncio.wait_for(run.runner, timeout=2)

        from router_mcp.web.views import run_from_log

        row = await db.get_request_log(run.result["log_id"])
        stored = run_from_log(row)
        titles = [step["title"] for step in stored["steps"]]
        assert "первый шаг" in titles and "второй шаг" in titles
        assert stored["steps"][-1]["kind"] == "log"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_unknown_run_is_a_404(tmp_path):
    db, runs = await _setup(tmp_path, SlowAdapter())
    try:
        with pytest.raises(NotFound):
            runs.get("nope")
    finally:
        await db.close()
