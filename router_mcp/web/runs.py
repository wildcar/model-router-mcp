"""The registry of runs in flight.

A run is one dispatch that the caller does not wait for. `POST /api/v1/runs`
answers immediately with an id; the console polls that id once a second until
the run is finished. Three things follow from doing it this way rather than by
holding the HTTP request open (§10.5):

  - closing the tab does not lose the run, and reopening it picks the run back
    up by id;
  - a run can be cancelled, because something in this process still holds its
    task;
  - nothing streams, so nginx, FastMCP and the retry logic are all untouched.

The registry is in-process and deliberately so: it is a view of what *this*
process is doing right now. The durable record is `request_logs`, which the
router writes as the run starts and finishes, and which is what the console
falls back to for a run this process has never heard of (after a restart, say).

Concurrency is capped. One SQLite connection behind one write lock serves the
whole service, and a six-model batch launched at once would spend its time
queueing on that lock (§21, open question 5). Runs above the cap sit in
`queued`, which is a status the glossary already had.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from router_mcp.domain.errors import Conflict, NotFound, RouterError
from router_mcp.domain.messages import RouterRequest
from router_mcp.web.views import error_envelope, run_from_dispatch

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Run:
    """One submitted run and everything the poller needs to draw it."""

    id: str
    task: str  # what was asked for, e.g. "text-to-image"
    modality: str  # the group it is billed in
    label: str  # what the operator picked: a model id, or a provider hint
    external_user_id: str
    status: str = "queued"  # queued | running | ok | error | cancelled
    batch_id: str | None = None
    client_request_id: str | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    # The live timeline: the router appends to this very list as it works.
    steps: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None  # the Run envelope, once finished
    error: dict[str, Any] | None = None  # the error envelope, if it failed
    # The coroutine executing this run. Named `runner`, not `task`: `task` is the
    # operation the caller asked for, and one name for both invited exactly the
    # bug where the asyncio object was serialized into the poll response.
    runner: asyncio.Task | None = None

    @property
    def log_id(self) -> int | None:
        """The log row, known from the first step the router records."""
        if self.result and self.result.get("log_id"):
            return int(self.result["log_id"])
        if self.error and self.error.get("error", {}).get("log_id"):
            return int(self.error["error"]["log_id"])
        for step in self.steps:
            if step.get("log_id"):
                return int(step["log_id"])
        return None

    @property
    def done(self) -> bool:
        return self.status in ("ok", "error", "cancelled")

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.id,
            "status": self.status,
            "task": self.task,
            "modality": self.modality,
            "label": self.label,
            "external_user_id": self.external_user_id,
            "batch_id": self.batch_id,
            "client_request_id": self.client_request_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_id": self.log_id,
            # Progress is the step list itself: the console already knows how to
            # draw a timeline, and a percentage would be invented — nothing
            # knows how many steps a run has left.
            "steps": list(self.steps),
            "result": self.result,
            "error": self.error,
        }


class RunRegistry:
    def __init__(self, core, max_concurrent: int = 3, keep: int = 200) -> None:
        self._core = core
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._by_key: dict[str, str] = {}  # client_request_id -> run_id
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._keep = max(10, keep)

    # --- submission -------------------------------------------------------
    async def submit(self, request: RouterRequest, *, label: str) -> Run:
        """Start a run and return immediately.

        A repeated `client_request_id` returns the run that key already
        produced, in this process or in the log. The double-submitted form and
        the client retrying after a dropped connection are the same event, and
        neither should be charged twice.
        """
        if request.client_request_id:
            existing = await self._find_by_key(request.client_request_id)
            if existing is not None:
                return existing

        run = Run(
            id=uuid.uuid4().hex,
            task=request.task.value,
            modality=request.modality.value,
            label=label,
            external_user_id=request.external_user_id,
            batch_id=request.batch_id,
            client_request_id=request.client_request_id,
        )
        self._runs[run.id] = run
        if request.client_request_id:
            self._by_key[request.client_request_id] = run.id
        self._evict()
        run.runner = asyncio.create_task(self._execute(run, request))
        return run

    async def _execute(self, run: Run, request: RouterRequest) -> None:
        async with self._semaphore:
            run.status = "running"
            run.started_at = _now()
            try:
                result = await self._core.router.dispatch(request, steps_out=run.steps)
                run.result = run_from_dispatch(result, request)
                run.status = "ok"
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.error = {
                    "error": {
                        "type": "Cancelled",
                        "code": "cancelled",
                        "message": "Прогон отменён оператором",
                        "log_id": run.log_id,
                        "retriable": True,
                    }
                }
                raise
            except RouterError as exc:
                run.status = "error"
                run.error = error_envelope(exc)
            except Exception as exc:  # pragma: no cover - normalized like dispatch
                logger.exception("Run %s failed unexpectedly", run.id)
                run.status = "error"
                run.error = error_envelope(RouterError(str(exc)))
            finally:
                run.finished_at = _now()
                run.runner = None

    # --- reading ----------------------------------------------------------
    def get(self, run_id: str) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise NotFound(f"Прогон {run_id} не найден")
        return run

    def list(
        self, *, batch_id: str | None = None, limit: int = 20
    ) -> list[Run]:
        """Newest first. This is the лента последних прогонов on «Прогон»."""
        runs = list(self._runs.values())
        if batch_id:
            runs = [r for r in runs if r.batch_id == batch_id]
        return list(reversed(runs))[:limit]

    def active_count(self) -> int:
        return sum(1 for r in self._runs.values() if not r.done)

    # --- cancellation -----------------------------------------------------
    async def cancel(self, run_id: str) -> Run:
        run = self.get(run_id)
        if run.done:
            raise Conflict(f"Прогон уже завершён со статусом {run.status!r}")
        task = run.runner
        if task is not None:
            task.cancel()
            # Wait for the task to finish unwinding: for a CLI run that is where
            # the subprocess is killed and the log row is closed, and answering
            # before it happened would report a state that is not true yet.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        run.status = "cancelled"
        run.finished_at = run.finished_at or _now()
        return run

    async def aclose(self) -> None:
        """Cancel everything still running, on shutdown."""
        for run in list(self._runs.values()):
            if run.runner is not None:
                run.runner.cancel()

    # --- internals --------------------------------------------------------
    async def _find_by_key(self, client_request_id: str) -> Run | None:
        run_id = self._by_key.get(client_request_id)
        if run_id and run_id in self._runs:
            return self._runs[run_id]
        row = await self._core.db.find_log_by_client_request_id(client_request_id)
        if row is None:
            return None
        # The run happened, but not in this process (or long enough ago to have
        # been evicted). Rebuild enough of it from the log to answer with the
        # stored result instead of running the request a second time.
        from router_mcp.web.views import run_from_log

        run = Run(
            id=uuid.uuid4().hex,
            task=str(row.get("task") or ""),
            modality=str(row.get("modality") or ""),
            label=str(row.get("model_id") or ""),
            external_user_id=str(row.get("external_user_id") or ""),
            status="ok" if row.get("status") == "ok" else str(row.get("status")),
            batch_id=row.get("batch_id"),
            client_request_id=client_request_id,
            created_at=str(row.get("created_at") or _now()),
            finished_at=row.get("finished_at"),
        )
        envelope = run_from_log(row)
        run.steps = list(envelope.get("steps") or [])
        if row.get("status") == "ok":
            run.result = envelope
        else:
            run.error = {
                "error": {
                    "type": "Replayed",
                    "code": "replayed",
                    "message": row.get("error_message") or "",
                    "log_id": row.get("id"),
                    "retriable": False,
                }
            }
        self._runs[run.id] = run
        self._by_key[client_request_id] = run.id
        self._evict()
        return run

    def _evict(self) -> None:
        """Drop the oldest finished runs once the registry is full."""
        while len(self._runs) > self._keep:
            for run_id, run in self._runs.items():
                if run.done:
                    del self._runs[run_id]
                    if run.client_request_id:
                        self._by_key.pop(run.client_request_id, None)
                    break
            else:  # every run is still going; nothing to evict
                return
