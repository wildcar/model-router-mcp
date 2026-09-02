"""The `local` provider: models that are computed in this very process.

Every other adapter here is an HTTP client (or a subprocess) talking to someone
else's machine. A model that lives beside the router as a file had nowhere to
plug in — and the rule that only `router-client` ever reaches a model means the
application on the other side must not load weights of its own. So this adapter
calls a Python function instead of an endpoint, and from the outside is one
more provider: the same `POST /api/v1/runs`, the same error envelope, the same
row in `usage` (with a price of exactly zero, which is a fact and not a gap).
ADR-0009 records the reasoning.

What is particular to computing here rather than there:

* **CPU time is shared with the router.** Inference runs in a small thread pool
  behind a semaphore (`LOCAL_MAX_CONCURRENCY`, default 2): a third request waits
  for a slot rather than starving the event loop that answers `chat`.
* **A run has a deadline** (`LOCAL_RUN_TIMEOUT_SECONDS`, 60) and answers
  `timeout`, retriable. A thread cannot be killed, so the overrun computation is
  left to finish and keeps its slot until it does.
* **Weights are an installation matter.** Missing files are `model_not_loaded`,
  a 503 nobody should retry — not a request fault and not a 500.
* **Nothing is fetched.** Attachments arrive as bytes (the router's blob store);
  a bare URL is refused rather than downloaded from a provider that is
  supposed to have no network at all.

Each model is a `LocalModel` in `MODELS`: its own inference function, its own
weight files, its own input fields. The first — and today the only — one is
UVDoc (`local_uvdoc.py`). The registry row's `input_map` and `extra_params`
work exactly as for Replicate: a role binds to a field the model declares, a
knob reaches the function only if the row allows it *and* the model knows it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from router_mcp.adapters import local_uvdoc
from router_mcp.adapters.base import ProviderAdapter, merge_params
from router_mcp.domain import progress
from router_mcp.domain.errors import (
    BadRequest,
    ComputeTimeout,
    ModelNotLoaded,
    ModelUnavailable,
    NotConfigured,
    ProviderError,
    RouterError,
)
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

# Import names, not distribution names: `find_spec` answers without importing,
# so `is_configured()` costs nothing and the router process does not load
# OpenCV and ONNX Runtime until a request actually needs them.
REQUIRED_PACKAGES = ("onnxruntime", "cv2", "numpy")
INSTALL_HINT = "pip install -e '.[local]'"

# `infer(handle, files, params) -> (bytes, mime, meta)`: the model's own function.
# `files` is field name -> bytes, in the model's vocabulary; `params` holds only
# the knobs the row allowed and the model declared.
Infer = Callable[[Any, dict[str, bytes], dict[str, Any]], tuple[bytes, str, dict[str, Any]]]
Load = Callable[[Path, int], Any]


@dataclass(frozen=True, slots=True)
class LocalModel:
    """One model this build can compute, described the way a registry row
    describes a hosted one."""

    model_id: str  # the registry model_id, e.g. "local/uvdoc"
    display_name: str
    task: Task
    # File fields in positional order; the first is the subject an attachment
    # with no role binds to. Roles bind through the row's `input_map`.
    inputs: tuple[str, ...]
    weight_files: tuple[str, ...]  # relative to the models directory
    load: Load
    infer: Infer
    description: str = ""
    # Knobs `infer` reads. A name the row allows but the model lacks is dropped
    # and reported, exactly as Replicate treats a property its schema lacks.
    params: tuple[str, ...] = ()
    model_card: str | None = None
    license: str = ""

    @property
    def required(self) -> tuple[str, ...]:
        return self.inputs


UVDOC = LocalModel(
    model_id=local_uvdoc.MODEL_ID,
    display_name=local_uvdoc.DISPLAY_NAME,
    task=Task.IMAGE_TO_IMAGE,
    inputs=("image",),
    weight_files=local_uvdoc.WEIGHT_FILES,
    load=local_uvdoc.load,
    infer=local_uvdoc.infer,
    description=local_uvdoc.DESCRIPTION,
    model_card=local_uvdoc.MODEL_CARD,
    license=local_uvdoc.LICENSE,
)

MODELS: dict[str, LocalModel] = {UVDOC.model_id: UVDOC}


class LocalAdapter(ProviderAdapter):
    name = "local"
    # No credential: the thing to check is whether the runtime is installed,
    # and `is_configured` says so. Missing weights are a per-model error.
    env_var = None
    supported_tasks = {Task.IMAGE_TO_IMAGE}
    # Nothing declared for any task: a local model's knobs come from the row's
    # `extra_params` alone (and only those the model itself knows).
    SUPPORTED_PARAMS: dict[Task, tuple[str, ...]] = {Task.IMAGE_TO_IMAGE: ()}
    # Files are bytes in this process; there is no inline/URL distinction.
    inline_file_limit_bytes = None

    def __init__(
        self,
        models_dir: str | Path,
        max_concurrency: int = 2,
        timeout_seconds: float = 60.0,
        models: dict[str, LocalModel] | None = None,
        load_blob: Callable[[str], bytes] | None = None,
    ) -> None:
        self._dir = Path(models_dir)
        self._models = dict(MODELS if models is None else models)
        self._limit = max(1, int(max_concurrency))
        self._timeout = float(timeout_seconds)
        # Only for an attachment that arrives as a blob ref without its bytes;
        # both current callers already load them. Injected so the adapter keeps
        # knowing nothing about the blob store's layout.
        self._load_blob = load_blob
        # As many workers as slots: the pool is the hard ceiling on threads
        # computing at once, the semaphore is what lets a queued run say so.
        self._pool = ThreadPoolExecutor(
            max_workers=self._limit, thread_name_prefix="local-model"
        )
        self._slots = asyncio.Semaphore(self._limit)
        self._handles: dict[str, Any] = {}
        self._loading: dict[str, asyncio.Lock] = {}
        # Cores per concurrent session, so two runs on a four-core host get two
        # threads each rather than fighting over eight.
        self._threads = max(1, (os.cpu_count() or 1) // self._limit)
        self._configured: bool | None = None
        tasks = {m.task for m in self._models.values()}
        if tasks:
            self.supported_tasks = tasks
            self.SUPPORTED_PARAMS = {task: () for task in tasks}

    @property
    def models(self) -> dict[str, LocalModel]:
        return dict(self._models)

    def is_configured(self) -> bool:
        """True when the runtime libraries are installed. Checked once: they do
        not appear or vanish while the process runs."""
        if self._configured is None:
            self._configured = all(
                importlib.util.find_spec(name) is not None for name in REQUIRED_PACKAGES
            )
        return self._configured

    async def list_catalog(self) -> list[RegistryModel] | None:
        # Fixed catalog: the rows are seeded at startup, nothing to ask.
        return None

    async def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # --- one run ------------------------------------------------------------
    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        spec = self._models.get(model.model_id)
        if spec is None:
            raise ModelUnavailable(
                f"No local model {model.model_id!r} in this build; available: "
                f"{', '.join(sorted(self._models)) or 'none'}"
            )
        if not self.is_configured():
            raise NotConfigured(
                f"Local models need {', '.join(REQUIRED_PACKAGES)} installed: {INSTALL_HINT}"
            )
        files, applied, dropped = self._bind_files(request, spec, model)
        params, dropped_params = self._bind_params(request, spec, model)
        dropped.extend(dropped_params)

        handle = await self._handle(spec)
        progress.emit(
            "local", "считаем на этом хосте",
            model=spec.model_id, threads=self._threads, inputs=sorted(files),
        )
        started = time.perf_counter()
        data, mime, meta = await self._run(spec, lambda: spec.infer(handle, files, params))
        elapsed = round(time.perf_counter() - started, 3)
        progress.emit("local", "готово", model=spec.model_id, compute_s=elapsed)

        return RouterResponse(
            task=request.task,
            provider=self.name,
            model_id=model.model_id,
            usage=Usage(
                images=1,
                # Our own wall clock: here the provider is us, and this is the
                # figure a hosted model would have reported as predict_time.
                compute_seconds=elapsed,
                raw={"compute_seconds": elapsed, "device": "cpu", "threads": self._threads},
            ),
            attachments=[Attachment(kind=_kind_of(mime), mime=mime, data=data)],
            raw_meta={
                "applied_input": applied,
                "dropped_params": dropped,
                "compute_seconds": elapsed,
                "local": meta,
            },
        )

    # --- inputs -------------------------------------------------------------
    def _bind_files(
        self, request: RouterRequest, spec: LocalModel, model: RegistryModel
    ) -> tuple[dict[str, bytes], dict[str, Any], list[str]]:
        """Attachments onto the model's own field names; text is reported, not used.

        Roles first, through the row's `input_map`, then loose attachments in
        order onto whatever field is still free — the same two rules as the
        Replicate adapter, against a declared tuple instead of an OpenAPI schema.
        A role that resolves to nothing is an error naming the real fields; a
        second loose attachment with no field left is skipped and counted.
        """
        files: dict[str, bytes] = {}
        applied: dict[str, Any] = {}
        dropped: list[str] = []

        text = (request.text or "").strip()
        if not text and request.messages:
            text = _last_user_message(request.messages)
        if text:
            # No local model reads a prompt. The task only *offered* the text,
            # so the run proceeds and `ignored_params` says where it went.
            dropped.append("prompt")

        roled = [att for att in request.attachments if att.role]
        loose = [att for att in request.attachments if not att.role]
        for att in roled:
            field = model.input_map.get(att.role or "") or att.role
            if field not in spec.inputs:
                raise ProviderError(
                    f"Local model {spec.model_id} has no input for attachment role "
                    f"{att.role!r} (its inputs are: {', '.join(spec.inputs)}). Map the "
                    "role onto one of them in the model's input_map."
                )
            files[field] = self._bytes_of(att)
            applied[field] = f"{att.mime}, role={att.role}"
        unbound = 0
        for att in loose:
            field = next((f for f in spec.inputs if f not in files), None)
            if field is None:
                unbound += 1
                continue
            files[field] = self._bytes_of(att)
            applied[field] = att.mime
        if unbound:
            applied["unbound_attachments"] = unbound

        missing = [f for f in spec.required if f not in files]
        if missing:
            raise BadRequest(
                f"Local model {spec.model_id} requires input {', '.join(missing)}; "
                f"the request bound {', '.join(sorted(files)) or 'nothing'}"
            )
        return files, applied, dropped

    def _bytes_of(self, att: Attachment) -> bytes:
        if att.data is not None:
            return att.data
        if att.ref and self._load_blob is not None:
            return self._load_blob(att.ref)
        raise BadRequest(
            "Local models take file bytes (upload to POST /api/v1/blobs first); "
            "a URL is not fetched by this provider"
        )

    def _bind_params(
        self, request: RouterRequest, spec: LocalModel, model: RegistryModel
    ) -> tuple[dict[str, Any], list[str]]:
        """Only what the row allows, and of that only what the model knows."""
        allowed = merge_params(self.SUPPORTED_PARAMS.get(request.task, ()), model)
        params: dict[str, Any] = {}
        dropped: list[str] = []
        for key in allowed:
            if key not in request.params:
                continue
            if key not in spec.params:
                dropped.append(key)
                continue
            params[key] = request.params[key]
        return params, dropped

    # --- the model in memory ---------------------------------------------------
    async def _handle(self, spec: LocalModel) -> Any:
        """The loaded model, loading it on first use and once only."""
        handle = self._handles.get(spec.model_id)
        if handle is not None:
            return handle
        lock = self._loading.setdefault(spec.model_id, asyncio.Lock())
        async with lock:
            handle = self._handles.get(spec.model_id)
            if handle is not None:
                return handle
            missing = [f for f in spec.weight_files if not (self._dir / f).is_file()]
            if missing:
                raise ModelNotLoaded(
                    f"Weights for {spec.model_id} are not installed: "
                    f"{', '.join(missing)} missing under the models directory "
                    "(LOCAL_MODELS_DIR). Run deploy/fetch-models.sh; retrying the "
                    "request will not help.",
                    hint={"kind": "fetch_models", "script": "deploy/fetch-models.sh",
                          "missing": missing},
                )
            progress.emit("local", "загружаем модель в память", model=spec.model_id)
            started = time.perf_counter()
            loop = asyncio.get_running_loop()
            try:
                handle = await loop.run_in_executor(
                    self._pool, spec.load, self._dir, self._threads
                )
            except Exception as exc:
                raise ModelNotLoaded(
                    f"Weights for {spec.model_id} could not be loaded: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self._handles[spec.model_id] = handle
            progress.emit(
                "local", "модель загружена",
                model=spec.model_id, load_ms=int((time.perf_counter() - started) * 1000),
            )
            return handle

    # --- running under the limit -------------------------------------------------
    async def _run(self, spec: LocalModel, fn: Callable[[], Any]) -> Any:
        """`fn` in the pool, inside a slot, under the deadline.

        The slot is released when the *thread* is done, not when the caller
        stops waiting: a run that timed out or was cancelled is still burning a
        core, and admitting another on top of it is how a four-core host ends
        up computing five things.
        """
        if self._slots.locked():
            progress.emit(
                "local", "ждём свободный слот",
                model=spec.model_id, limit=self._limit,
            )
        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._pool, fn)
        detached = False
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
        except asyncio.TimeoutError:
            detached = True
            self._release_when_done(future)
            raise ComputeTimeout(
                f"{spec.model_id} did not finish within {self._timeout:g}s on this "
                "host; the computation is left to finish and its slot stays taken "
                "until it does"
            ) from None
        except asyncio.CancelledError:
            detached = True
            self._release_when_done(future)
            raise
        except RouterError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"{spec.model_id} failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            if not detached:
                self._slots.release()

    def _release_when_done(self, future: asyncio.Future) -> None:
        def _done(f: asyncio.Future) -> None:
            self._slots.release()
            if not f.cancelled() and f.exception() is not None:
                logger.warning("Abandoned local run ended with %r", f.exception())

        future.add_done_callback(_done)


def _last_user_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") in (None, "user"):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def _kind_of(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


__all__ = ["LocalAdapter", "LocalModel", "MODELS", "UVDOC", "REQUIRED_PACKAGES"]
