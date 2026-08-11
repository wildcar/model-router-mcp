"""Replicate adapter — asynchronous predictions over one API for many tasks.

Three things here are new to this codebase, and ADR-0007 records why:

* **The call is a job, not a request.** `POST /predictions` returns an id; the
  answer arrives later. `Prefer: wait` collapses that into one round trip when
  the model is fast, and the poll loop handles the rest.
* **Results are fetched into the blob store.** Replicate's delivery URLs expire
  in about an hour, and an attachment with a URL and no bytes means the log entry
  an operator opens tomorrow points at nothing.
* **Most models bill per second of the provider's own compute**, metered from
  `metrics.predict_time`. That is a different quantity from seconds of audio and
  has its own rate (`RegistryModel.compute_price_per_second`).

Replicate is also the provider that most needs ADR-0006: `flux-schnell` and
`flux-kontext-pro` sit behind the same key, the same API shape and the same
billing unit, and differ only by task. The catalog is an allowlist, not a mirror
— the platform publishes thousands of models and a registry is a curated list.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from router_mcp.adapters.base import ProviderAdapter, RetryClientMixin, merge_params
from router_mcp.domain import progress
from router_mcp.domain.errors import (
    AccessDenied,
    ModelUnavailable,
    NotConfigured,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.task import Media, Task
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

PRICING_PAGE = "https://replicate.com/pricing"

# Terminal prediction states. Anything else means "still going".
TERMINAL = {"succeeded", "failed", "canceled"}

# "That version is gone", as Replicate phrases it. Matching on the text is
# unpleasant, but an author pushing new weights is the normal case and the only
# signal is the message.
STALE_VERSION_RE = re.compile(
    r"version.{0,40}(not (exist|found)|invalid|no longer)", re.IGNORECASE | re.DOTALL
)

# How a normalized field maps onto a model's own input property. Ordered: the
# first name the schema actually has wins. Every model names these differently
# and there is no convention to lean on.
PROMPT_KEYS = ("prompt", "text", "text_prompt", "input_text", "description", "query")
IMAGE_KEYS = (
    "image", "input_image", "image_input", "img", "image_url",
    "subject_image", "control_image", "first_frame_image", "start_image",
)
AUDIO_KEYS = ("audio", "audio_file", "audio_input", "audio_url", "speech")
SYSTEM_KEYS = ("system_prompt", "system")

# The property an attachment `role` lands on when the registry row does not say.
# A guess, and a narrow one: only names that can mean nothing else for that role.
# `RegistryModel.input_map` overrides it, and an unresolved role is an error
# rather than a positional guess — binding a garment to `human_img` is the bug
# roles exist to remove, and doing it silently is worse than saying so.
ROLE_KEYS: dict[str, tuple[str, ...]] = {
    "image": IMAGE_KEYS,
    "mask": ("mask", "mask_image", "image_mask"),
    "face": ("swap_image", "face_image"),
    "garment": ("garm_img", "garment_image", "garment", "cloth_image"),
    "reference": ("reference_image", "ref_image"),
    "style": ("style_image", "style_reference_image"),
}

# Data URIs are the only inline form the prediction call takes, and only up to
# this size. Anything bigger has to be a URL — see `PUBLIC_BLOB_BASE_URL`.
DATA_URI_LIMIT = 256 * 1024

# Elapsed-time boundaries at which the poll loop puts a heartbeat on the
# timeline. Deliberately not one step per poll: the router caps a run at 500
# steps, and a ten-minute video would otherwise spend a tenth of that budget
# saying "still going". After the last boundary it is every five minutes.
STEP_LADDER = (5, 10, 20, 40, 80, 160, 320)
STEP_INTERVAL_AFTER = 300


@dataclass(slots=True)
class ModelInfo:
    """One allowlisted model, as its own document describes it."""

    slug: str  # "owner/name" — this is the registry model_id
    version_id: str | None  # None for official models, which take the named route
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    display_name: str = ""
    task: Task = Task.TEXT_TO_IMAGE
    fetched_at: float = 0.0

    @property
    def properties(self) -> dict[str, Any]:
        props = self.input_schema.get("properties")
        return props if isinstance(props, dict) else {}

    @property
    def required(self) -> set[str]:
        return set(self.input_schema.get("required") or [])


class ReplicateAdapter(RetryClientMixin, ProviderAdapter):
    name = "replicate"
    env_var = "REPLICATE_API_TOKEN"
    # Which of these are actually reachable depends on `REPLICATE_MODELS`; the
    # declaration says what the adapter can drive, and the registry says what is
    # configured. Editing an image is here because Replicate genuinely has models
    # for it — which is the point of the whole taxonomy.
    supported_tasks = {
        Task.TEXT_TO_IMAGE,
        Task.IMAGE_TO_IMAGE,
        Task.IMAGE_TO_TEXT,
        Task.TEXT_TO_TEXT,
        Task.TEXT_TO_AUDIO,
        Task.SPEECH_TO_TEXT,
        Task.TEXT_TO_SPEECH,
        Task.TEXT_TO_VIDEO,
        Task.IMAGE_TO_VIDEO,
    }
    # Above this, a file has to be a URL Replicate can fetch. Declared so the
    # console can warn before the first real request instead of after it.
    inline_file_limit_bytes = DATA_URI_LIMIT

    # Declared knobs, filtered per model against its own schema: a name that this
    # model has no property for is dropped and reported, never sent. A wide-open
    # passthrough would forfeit that, leave the console with no form to draw, and
    # let a caller set `num_outputs: 50` on the operator's money.
    _IMAGE_PARAMS = (
        "negative_prompt", "aspect_ratio", "width", "height", "num_outputs",
        "num_inference_steps", "guidance_scale", "seed", "output_format",
        "output_quality",
    )
    _EDIT_PARAMS = _IMAGE_PARAMS + ("strength", "mask")
    _VIDEO_PARAMS = ("negative_prompt", "aspect_ratio", "num_frames", "fps", "seed")
    _AUDIO_PARAMS = ("duration", "seed", "output_format")
    SUPPORTED_PARAMS = {
        Task.TEXT_TO_IMAGE: _IMAGE_PARAMS,
        Task.IMAGE_TO_IMAGE: _EDIT_PARAMS,
        Task.IMAGE_TO_TEXT: ("system_prompt", "temperature", "max_tokens", "top_p"),
        Task.TEXT_TO_TEXT: ("system_prompt", "temperature", "max_tokens", "top_p", "seed"),
        Task.TEXT_TO_AUDIO: _AUDIO_PARAMS,
        Task.SPEECH_TO_TEXT: ("language", "translate"),
        Task.TEXT_TO_SPEECH: ("voice", "speed", "seed"),
        Task.TEXT_TO_VIDEO: _VIDEO_PARAMS,
        Task.IMAGE_TO_VIDEO: _VIDEO_PARAMS,
    }

    def __init__(
        self,
        api_token: str | None,
        base_url: str,
        models: str = "",
        wait_seconds: int = 60,
        poll_deadline_seconds: int = 900,
        poll_min_interval: float = 1.0,
        poll_max_interval: float = 10.0,
        model_ttl_seconds: int = 3600,
        max_output_mb: float = 32.0,
        max_output_files: int = 8,
        stream_logs: bool = True,
        public_blob_url: Any = None,
        timeout_seconds: float = 60.0,
        retries: int = 2,
    ) -> None:
        self._api_token = api_token
        self._slugs = parse_models(models)
        # The API caps `Prefer: wait` at 60 seconds.
        self._wait_seconds = max(1, min(int(wait_seconds), 60))
        self._deadline = poll_deadline_seconds
        self._poll_min = poll_min_interval
        self._poll_max = poll_max_interval
        self._ttl = model_ttl_seconds
        self._max_output_bytes = int(max_output_mb * 1024 * 1024)
        self._max_output_files = max_output_files
        self._stream_logs = stream_logs
        # Turns a blob ref into a URL Replicate can fetch, or None when no public
        # host is configured. Injected rather than imported so the adapter keeps
        # knowing nothing about the web layer.
        self._public_blob_url = public_blob_url
        self._retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_token or ''}",
                "Content-Type": "application/json",
            },
        )
        # A second client with no credentials: output files live on
        # replicate.delivery, a different host, and httpx sends client-level
        # default headers to absolute URLs too. Downloading through the first
        # client would hand the API token to a CDN.
        self._files = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True)
        self._infos: dict[str, ModelInfo] = {}
        self._info_lock = asyncio.Lock()

    def is_configured(self) -> bool:
        return bool(self._api_token)

    async def close(self) -> None:
        await self._client.aclose()
        await self._files.aclose()

    # --- one prediction ---------------------------------------------------
    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        if not self._api_token:
            raise NotConfigured("REPLICATE_API_TOKEN is not set")
        info = await self._model_info(model.model_id)
        payload, dropped = await self._build_input(request, info, model)

        progress.emit(
            "replicate", "создаём предсказание",
            model=info.slug, version=(info.version_id or "official")[:12],
        )
        try:
            prediction = await self._create(info, payload)
        except (ProviderError, ModelUnavailable) as exc:
            if not STALE_VERSION_RE.search(str(exc)):
                raise
            # The author pushed new weights. Without this every community model
            # breaks silently until the process restarts.
            logger.info("Replicate version for %s is stale, refetching", info.slug)
            info = await self._model_info(info.slug, force=True)
            payload, dropped = await self._build_input(request, info, model)
            prediction = await self._create(info, payload)

        if _status(prediction) not in TERMINAL:
            prediction = await self._poll(prediction)
        return await self._to_response(request, model, info, prediction, payload, dropped)

    async def _create(self, info: ModelInfo, payload: dict[str, Any]) -> dict[str, Any]:
        if info.version_id:
            url, body = "/predictions", {"version": info.version_id, "input": payload}
        else:
            owner, _, model_name = info.slug.partition("/")
            url = f"/models/{owner}/{model_name}/predictions"
            body = {"input": payload}
        headers = {
            "Prefer": f"wait={self._wait_seconds}",
            # A server-side deadline, so a prediction this process abandons (a
            # restart, a kill) stops burning compute instead of running to
            # completion and billing for an answer nobody will read.
            "Cancel-After": f"{self._deadline}s",
        }
        last: Exception | None = None
        for attempt in range(1, self._retries + 2):
            try:
                response = await self._request_with_retry(
                    "POST", url, json=body, headers=headers,
                    # Deliberately no retries: a retried create makes a second
                    # billable prediction that nobody will collect.
                    retries=0,
                    timeout=httpx.Timeout(self._wait_seconds + 15, connect=10),
                )
                return response.json()
            except RateLimited as exc:
                # 429 is the one failure that means nothing was created.
                last = exc
                if attempt > self._retries:
                    raise
                await asyncio.sleep(exc.retry_after_s or 0.4 * attempt)
        raise last or ProviderUnavailable("Replicate did not accept the prediction")

    async def _poll(self, prediction: dict[str, Any]) -> dict[str, Any]:
        url = (prediction.get("urls") or {}).get("get") or f"/predictions/{prediction.get('id')}"
        prediction_id = str(prediction.get("id") or "")
        started = time.monotonic()
        interval = self._poll_min
        next_step = 0
        last_status = _status(prediction)
        try:
            while True:
                await asyncio.sleep(interval)
                interval = min(interval * 1.5, self._poll_max)
                elapsed = time.monotonic() - started

                response = await self._request_with_retry("GET", url)
                prediction = response.json()
                status = _status(prediction)
                if status != last_status:
                    last_status = status
                    progress.emit(
                        "replicate", "модель считает",
                        prediction_id=prediction_id, status=status,
                    )
                elif elapsed >= _next_boundary(next_step):
                    next_step += 1
                    progress.emit(
                        "replicate", f"выполняется, {_human(elapsed)}",
                        prediction_id=prediction_id, status=status,
                        elapsed_s=round(elapsed), log_tail=self._log_tail(prediction),
                    )
                if status in TERMINAL:
                    return prediction
                if elapsed > self._deadline:
                    await self._cancel_quietly(prediction_id)
                    raise ProviderTimeout(
                        f"Replicate prediction {prediction_id} did not finish "
                        f"within {self._deadline}s"
                    )
        except asyncio.CancelledError:
            # The run is being torn down, and Replicate keeps charging by the
            # second until told to stop. Shielded so the cancel itself survives.
            progress.emit("replicate", "отменяем предсказание", prediction_id=prediction_id)
            await self._cancel_quietly(prediction_id)
            raise

    async def _cancel_quietly(self, prediction_id: str) -> None:
        """Best effort, five seconds, never raises: whatever went wrong, the run
        is already ending and this is only about stopping the meter."""
        if not prediction_id:
            return
        try:
            await asyncio.shield(
                asyncio.wait_for(
                    self._request_with_retry(
                        "POST", f"/predictions/{prediction_id}/cancel", retries=0
                    ),
                    timeout=5,
                )
            )
        except Exception:  # noqa: BLE001 - reported, never propagated
            logger.warning("Could not cancel Replicate prediction %s", prediction_id)

    def _log_tail(self, prediction: dict[str, Any]) -> str | None:
        """The model's own last line of output. Diffusers print a progress bar
        there, and for someone watching a minute tick by it is the most useful
        thing on screen. Never the whole log: it grows to hundreds of KB and
        would be stored in `plan_json`."""
        if not self._stream_logs:
            return None
        raw = prediction.get("logs")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return raw.strip().splitlines()[-1][:200]

    # --- the answer -------------------------------------------------------
    async def _to_response(
        self,
        request: RouterRequest,
        model: RegistryModel,
        info: ModelInfo,
        prediction: dict[str, Any],
        payload: dict[str, Any],
        dropped: list[str],
    ) -> RouterResponse:
        status = _status(prediction)
        error = prediction.get("error")
        prediction_id = str(prediction.get("id") or "")
        if status == "failed":
            tail = self._log_tail(prediction)
            detail = f"{error or 'no reason given'}"
            raise ProviderError(
                f"Replicate prediction failed: {detail[:300]}"
                + (f" (log: {tail})" if tail else "")
            )
        if status == "canceled":
            # Either our deadline header fired or somebody cancelled it upstream.
            raise ProviderTimeout(
                f"Replicate cancelled prediction {prediction_id} after {self._deadline}s"
            )

        text, urls = self._collect_outputs(prediction.get("output"))
        if not text and not urls:
            logger.warning(
                "Replicate parse miss model=%s output=%r", info.slug,
                str(prediction.get("output"))[:600],
            )
            raise ProviderError(f"Replicate model {info.slug} returned no output")

        attachments, skipped = await self._fetch_outputs(urls)
        metrics = prediction.get("metrics") or {}
        images = sum(1 for a in attachments if a.kind == "image") or None
        return RouterResponse(
            task=request.task,
            provider=self.name,
            model_id=info.slug,
            usage=Usage(
                # The billable figure. `total_time` includes queue time, which
                # Replicate does not charge for, and is kept for latency analysis.
                compute_seconds=_float_or_none(metrics.get("predict_time")),
                prompt_tokens=_int_or_none(metrics.get("input_token_count")),
                completion_tokens=_int_or_none(metrics.get("output_token_count")),
                total_tokens=_sum_or_none(
                    metrics.get("input_token_count"), metrics.get("output_token_count")
                ),
                images=images,
                raw={"metrics": metrics, "status": status, "prediction_id": prediction_id},
            ),
            text=text or None,
            attachments=attachments,
            raw_meta={
                "provider_request_id": prediction_id,
                "version": info.version_id,
                "task": info.task.value,
                # Three distinct facts, which no other adapter here reports: what
                # went on the wire, what was declared but this model has no
                # property for, and what could not be brought home.
                "applied_input": {k: _short(v) for k, v in payload.items()},
                "dropped_params": dropped,
                "outputs_not_downloaded": skipped,
                "predict_time": metrics.get("predict_time"),
                "total_time": metrics.get("total_time"),
            },
        )

    @classmethod
    def _collect_outputs(cls, output: Any) -> tuple[str, list[str]]:
        """`(text, urls)` out of whatever shape this model answers in.

        Four of them: one URL, a list of URLs, one string of text, and a list of
        text fragments (a streamed LLM answer). The last is joined with *nothing*
        — those are token pieces, and a newline between them mangles every answer.
        """
        if output is None:
            return "", []
        if isinstance(output, str):
            return ("", [output]) if _is_url(output) else (output, [])
        if isinstance(output, list):
            items = [i for i in output if i not in (None, "")]
            if not items:
                return "", []
            if all(isinstance(i, str) for i in items):
                first = items[0]
                if _is_url(first):
                    return "", [i for i in items if _is_url(i)]
                return "".join(items), []
            text_parts, urls = [], []
            for item in items:
                sub_text, sub_urls = cls._collect_outputs(item)
                if sub_text:
                    text_parts.append(sub_text)
                urls.extend(sub_urls)
            return "".join(text_parts), urls
        if isinstance(output, dict):
            text_parts, urls = [], []
            for value in output.values():
                sub_text, sub_urls = cls._collect_outputs(value)
                if sub_text:
                    text_parts.append(sub_text)
                urls.extend(sub_urls)
            return "".join(text_parts), urls
        return "", []

    async def _fetch_outputs(
        self, urls: list[str]
    ) -> tuple[list[Attachment], list[dict[str, Any]]]:
        """Bring the results home, within bounds.

        Sequentially, not gathered: four 8 MB images fetched in parallel is 32 MB
        resident plus four blob writes racing for one SQLite write lock.
        """
        attachments: list[Attachment] = []
        skipped: list[dict[str, Any]] = []
        for index, url in enumerate(urls, start=1):
            mime = _mime_of(url)
            if index > self._max_output_files:
                skipped.append({"url": url, "reason": "превышен лимит числа файлов"})
                attachments.append(Attachment(kind=_kind_of(mime), mime=mime, url=url))
                continue
            progress.emit(
                "replicate", f"скачиваем результат {index}/{len(urls)}", url=url
            )
            try:
                data, mime = await self._download(url)
            except _TooLarge as exc:
                progress.emit(
                    "replicate", f"результат не скачан: {exc}", url=url, reason=str(exc)
                )
                skipped.append({"url": url, "reason": str(exc)})
                attachments.append(Attachment(kind=_kind_of(mime), mime=mime, url=url))
                continue
            except Exception as exc:  # noqa: BLE001 - the money is already spent
                logger.warning("Replicate output download failed: %s", exc)
                skipped.append({"url": url, "reason": f"не скачался: {exc}"[:200]})
                attachments.append(Attachment(kind=_kind_of(mime), mime=mime, url=url))
                continue
            attachments.append(
                Attachment(kind=_kind_of(mime), mime=mime, data=data, url=url)
            )
        return attachments, skipped

    async def _download(self, url: str) -> tuple[bytes, str]:
        async with self._files.stream("GET", url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > self._max_output_bytes:
                raise _TooLarge(_size_message(int(declared), self._max_output_bytes))
            mime = (response.headers.get("content-type") or "").split(";")[0].strip()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                # A chunked response has no length header, so the cap is also
                # enforced while reading.
                if total > self._max_output_bytes:
                    raise _TooLarge(_size_message(total, self._max_output_bytes))
                chunks.append(chunk)
        return b"".join(chunks), mime or _mime_of(url)

    # --- input ------------------------------------------------------------
    async def _build_input(
        self,
        request: RouterRequest,
        info: ModelInfo,
        row: RegistryModel | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Normalized fields onto this model's own property names, plus the
        declared knobs its schema actually has.

        Two things come off the registry row and nothing else does: `input_map`,
        which says what each attachment role binds to, and `extra_params`, the
        per-model allowlist of knobs on top of the adapter's own. Without a row
        (the tests, and any caller that has none) attachments bind positionally
        and only the adapter's declared parameters are read.
        """
        props = info.properties
        input_map = row.input_map if row else {}
        payload: dict[str, Any] = {}
        dropped: list[str] = []

        text = (request.text or "").strip()
        if not text and request.messages:
            text = _last_user_message(request.messages)
        prompt_key = _first_present(PROMPT_KEYS, props)
        if text:
            if prompt_key is None:
                if Media.TEXT in request.task.signature.requires:
                    # The caller cannot get what they asked for: a generator with
                    # no prompt property cannot generate *this*. Naming the real
                    # properties is the reason the schema is read at all — the
                    # alternative is a 422 the operator cannot act on.
                    raise ProviderError(
                        f"Replicate model {info.slug} takes no prompt-like input "
                        f"(its inputs are: {', '.join(sorted(props)) or 'none'})"
                    )
                # Text the task only ever offered, on a model with nowhere to put
                # it. "Убери фон" is not an instruction a background remover
                # needs; refusing the run over it turns a working model into a
                # 500. Reported like any other knob this model does not have.
                dropped.append("prompt")
            else:
                payload[prompt_key] = text
        elif prompt_key and prompt_key in info.required:
            raise ProviderError(f"Replicate model {info.slug} requires a prompt")

        system_key = _first_present(SYSTEM_KEYS, props)
        if system_key and request.messages:
            system = _system_message(request.messages)
            if system:
                payload[system_key] = system

        def _bind(key: str, ref: str) -> None:
            # An array-typed property collects every file bound to it: the
            # multi-reference models (nano-banana-2, seedream-4) declare
            # `image_input` as a list of urls, and a bare string there is a 422.
            if (props.get(key) or {}).get("type") == "array":
                payload.setdefault(key, []).append(ref)
            else:
                payload[key] = ref

        def _taken() -> set[str]:
            # A filled array property stays open, so the second reference joins
            # the first instead of being dropped for want of another property.
            return {
                k for k in payload if (props.get(k) or {}).get("type") != "array"
            }

        # Roles first: a role names the property it must land on, and a
        # positionally bound attachment walking the same list would otherwise
        # take it first and leave the named one with nowhere to go.
        roled = [att for att in request.attachments if att.role]
        loose = [att for att in request.attachments if not att.role]
        for att in roled:
            key = _role_key(att.role or "", props, input_map, taken=_taken())
            if key is None:
                raise ProviderError(
                    f"Replicate model {info.slug} has no input for attachment role "
                    f"{att.role!r} (its inputs are: "
                    f"{', '.join(sorted(props)) or 'none'}). Map the role onto one "
                    f"of them in the model's input_map."
                )
            _bind(key, await self._file_reference(att, info))
        for att in loose:
            keys = IMAGE_KEYS if att.kind == "image" else AUDIO_KEYS
            key = _first_present(keys, props, taken=_taken())
            if key is None:
                continue
            _bind(key, await self._file_reference(att, info))

        # The adapter's declared knobs plus this row's allowlist. Widening the
        # set does not widen the gate: a name lands in the payload only if the
        # model's own schema has a property for it, and only after `_checked`
        # has held the value against that property's enum and range. And the
        # payload is `input` — a model property, never an API control.
        declared = merge_params(self.SUPPORTED_PARAMS.get(request.task, ()), row)
        for key in declared:
            if key not in request.params:
                continue
            if key not in props:
                dropped.append(key)
                continue
            payload[key] = _checked(key, request.params[key], props[key], info.slug)
        return payload, dropped

    async def _file_reference(self, att: Attachment, info: ModelInfo) -> str:
        """A URL or a data URI — the only two forms the prediction call takes."""
        if att.url:
            return att.url
        if att.ref and self._public_blob_url is not None:
            url = self._public_blob_url(att.ref)
            if url:
                return url
        if att.data is None:
            raise ProviderError("Attachment carries neither bytes nor a URL")
        if len(att.data) <= DATA_URI_LIMIT:
            import base64

            return f"data:{att.mime};base64,{base64.b64encode(att.data).decode()}"
        raise ProviderError(
            f"Replicate takes file inputs as URLs or data URIs up to 256 КБ; this one "
            f"is {len(att.data) // 1024} КБ. Set PUBLIC_BLOB_BASE_URL so the file can "
            f"be handed over by link, or pass a URL."
        )

    # --- catalog ----------------------------------------------------------
    async def list_catalog(self) -> list[RegistryModel] | None:
        """The allowlist, each model fetched by name.

        `None` rather than `[]` on an empty allowlist: an empty list tells the
        sync to deprecate every row this provider has, including the ones an
        operator added by hand.
        """
        if not self.is_configured() or not self._slugs:
            return None
        rows: list[RegistryModel] = []
        for slug, declared_task in self._slugs:
            try:
                info = await self._fetch_info(slug, declared_task)
            except ModelUnavailable:
                # A genuine 404: the model is gone, and omitting it is how the
                # sync learns to deprecate that row.
                logger.warning("Replicate model %s no longer exists", slug)
                continue
            rows.append(self._registry_row(info))
            self._infos[slug] = info
        return rows

    async def _model_info(self, slug: str, force: bool = False) -> ModelInfo:
        async with self._info_lock:
            cached = self._infos.get(slug)
            fresh = cached and (time.monotonic() - cached.fetched_at) < self._ttl
            if cached and fresh and not force:
                return cached
            declared = dict(self._slugs).get(slug)
            info = await self._fetch_info(slug, declared)
            self._infos[slug] = info
            return info

    async def _fetch_info(self, slug: str, declared_task: Task | None) -> ModelInfo:
        owner, _, model_name = slug.partition("/")
        if not owner or not model_name:
            raise ProviderError(f"Replicate model id must be owner/name, got {slug!r}")
        response = await self._request_with_retry("GET", f"/models/{owner}/{model_name}")
        data = response.json()
        version = data.get("latest_version") or {}
        schema = version.get("openapi_schema") or {}
        schemas = (schema.get("components") or {}).get("schemas") or {}
        input_schema = schemas.get("Input") or {}
        output_schema = schemas.get("Output") or {}
        return ModelInfo(
            slug=slug,
            version_id=version.get("id"),
            input_schema=input_schema,
            output_schema=output_schema,
            display_name=str(data.get("name") or slug),
            task=declared_task or infer_task(input_schema, output_schema, data),
            fetched_at=time.monotonic(),
        )

    def _registry_row(self, info: ModelInfo) -> RegistryModel:
        owner, _, _ = info.slug.partition("/")
        return RegistryModel(
            service_provider_id=self.name,
            model_provider=owner,
            model_id=info.slug,
            display_name=info.display_name or info.slug,
            tasks=(info.task,),
            # Replicate publishes no price through the API, so the row says so
            # rather than storing a zero. `price_is_unknown` then keeps it out of
            # automatic selection and off the fallback ladder until an operator
            # fills the rate in, and a run that consumes compute is logged
            # `unpriced` instead of free.
            price_source=f"page:https://replicate.com/{info.slug}",
            price_source_kind="unknown",
            price_checked_at=None,
            # Explicitly, because `compute_tier(0, 0)` says "free" and a free
            # video model would win every request that asked for the free tier.
            tier="expensive",
            source="catalog",
            is_active=True,
        )


# --- module-level helpers (pure, so the tests can drive them) --------------
class _TooLarge(Exception):
    """An output bigger than the cap. Not a failure of the run: the money is
    already spent and the link still works for about an hour."""


def parse_models(raw: str) -> list[tuple[str, Task | None]]:
    """`owner/name` or `owner/name:task`, comma separated."""
    out: list[tuple[str, Task | None]] = []
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        slug, sep, task = item.partition(":")
        slug = slug.strip()
        if not slug:
            continue
        parsed: Task | None = None
        if sep and task.strip():
            try:
                parsed = Task.from_str(task)
            except ValueError:
                logger.warning("Unknown task %r for Replicate model %s", task, slug)
        out.append((slug, parsed))
    return out


def infer_task(
    input_schema: dict[str, Any], output_schema: dict[str, Any], data: dict[str, Any]
) -> Task:
    """Guess the task from the model's schema, when the operator did not say.

    A guess, and treated as one: the `:task` suffix in `REPLICATE_MODELS` exists
    because a schema cannot always tell an upscaler from a generator.
    """
    props = input_schema.get("properties") or {}
    takes_image = any(k in props for k in IMAGE_KEYS)
    takes_audio = any(k in props for k in AUDIO_KEYS)
    blob = f"{data.get('description') or ''} {output_schema}".lower()

    produces_file = "uri" in str(output_schema).lower()
    if takes_audio and not produces_file:
        return Task.SPEECH_TO_TEXT
    if produces_file:
        if any(word in blob for word in ("video", "mp4", "frame")):
            return Task.IMAGE_TO_VIDEO if takes_image else Task.TEXT_TO_VIDEO
        if any(word in blob for word in ("audio", "music", "wav", "mp3")):
            return Task.TEXT_TO_AUDIO
        return Task.IMAGE_TO_IMAGE if takes_image else Task.TEXT_TO_IMAGE
    return Task.IMAGE_TO_TEXT if takes_image else Task.TEXT_TO_TEXT


def _first_present(
    keys: tuple[str, ...], props: dict[str, Any], taken: set[str] | None = None
) -> str | None:
    for key in keys:
        if key in props and key not in (taken or ()):
            return key
    return None


def _role_key(
    role: str, props: dict[str, Any], input_map: dict[str, str], taken: set[str]
) -> str | None:
    """The property an attachment role binds to, or None if this model has none.

    Three sources, most specific first: what an operator wrote on the registry
    row, the role used as a property name outright (`mask` really is called
    `mask` on most inpainting models), then the narrow guess table. An operator's
    mapping is not second-guessed — a name they chose that this model does not
    have is their typo to see, not something to quietly paper over with a guess.
    """
    mapped = input_map.get(role)
    if mapped:
        return mapped if mapped in props else None
    return _first_present((role, *ROLE_KEYS.get(role, ())), props, taken=taken)


def _checked(name: str, value: Any, spec: dict[str, Any], slug: str) -> Any:
    """Range and enum checks against the model's own schema.

    Locally, so a bad value fails with the allowed set in the message instead of
    costing a round trip and coming back as an opaque 422.
    """
    allowed = spec.get("enum")
    if isinstance(allowed, list) and allowed and value not in allowed:
        raise ProviderError(
            f"{name}={value!r} is not accepted by {slug}; allowed: "
            f"{', '.join(str(a) for a in allowed)}"
        )
    for bound, compare in (("minimum", True), ("maximum", False)):
        limit = spec.get(bound)
        if limit is None or not isinstance(value, (int, float)):
            continue
        if (compare and value < limit) or (not compare and value > limit):
            raise ProviderError(f"{name}={value} is outside {slug}'s range for {bound} {limit}")
    return value


def _last_user_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") in (None, "user"):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def _system_message(messages: list[dict[str, Any]]) -> str:
    parts = [
        str(m.get("content"))
        for m in messages
        if m.get("role") == "system" and isinstance(m.get("content"), str)
    ]
    return "\n".join(parts)


def _status(prediction: dict[str, Any]) -> str:
    return str(prediction.get("status") or "")


def _is_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text.startswith("data:"):
        return True
    parsed = urlparse(text)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _mime_of(url: str) -> str:
    return mimetypes.guess_type(urlparse(url).path)[0] or "application/octet-stream"


def _kind_of(mime: str) -> str:
    """`Attachment.kind` is a three-value contract, so video lands as a file and
    the mime carries the rest."""
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def _next_boundary(step: int) -> float:
    if step < len(STEP_LADDER):
        return float(STEP_LADDER[step])
    return float(STEP_LADDER[-1] + STEP_INTERVAL_AFTER * (step - len(STEP_LADDER) + 1))


def _human(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total} с"
    return f"{total // 60} мин {total % 60} с"


def _size_message(size: int, cap: int) -> str:
    return f"{size // (1024 * 1024)} МБ больше лимита {cap // (1024 * 1024)} МБ"


def _short(value: Any) -> Any:
    text = str(value)
    return text[:200] + "…" if len(text) > 200 else value


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sum_or_none(a: Any, b: Any) -> int | None:
    left, right = _int_or_none(a), _int_or_none(b)
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


# `AccessDenied` is raised by the shared mixin on 401/403; named here so the
# import is not flagged as unused and the contract is visible in this file.
__all__ = ["ReplicateAdapter", "AccessDenied", "parse_models", "infer_task"]
