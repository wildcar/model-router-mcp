"""JSON API consumed by the web UI.

Mirrors the MCP tools but is multipart-friendly (file uploads in later phases)
and adds registry-admin CRUD. Shares the same AppCore as the MCP tools.

Mounted twice: at `/api/v1` (the contract) and at `/api` (what the current
console still calls). Lists answer with the `{items, total, next_cursor, facets}`
envelope, runs with the `Run` envelope from `web/views.py`, failures with the
error envelope — a bare array or a bare number has nowhere to carry the total,
the cursor or the provenance of a price.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel

from router_mcp.domain.errors import BadRequest, Conflict, NotConfigured, NotFound
from router_mcp.domain.messages import (
    ATTACHMENT_ROLES,
    Attachment,
    FallbackPolicy,
    RouterRequest,
)
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import (
    Media,
    Task,
    media_of,
    resolve_task,
    validate_task_inputs,
)
from router_mcp.mcp_server.serialize import model_to_dict
from router_mcp.registry.models import PriceSchedule, RegistryModel
from router_mcp.runtime import get_core
from router_mcp.web import capabilities, health, mcp_summary, public_blobs, retention, thumbs
from router_mcp.web.views import image_card, page, run_from_dispatch, run_from_log

api_router = APIRouter()
blob_router = APIRouter()

# What a model input property can be called. Deliberately strict: the value is
# stored in a comma-joined column and ends up as a key in a provider's JSON body.
_PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# The keys of OpenRouter's `provider` routing object this console lets an
# operator set, by the shape each must have. A typo such as `onyl` would be
# ignored by OpenRouter without a word, and the model would go on being served
# by every host at every price, so an unknown key is refused here instead.
_ROUTING_LIST_KEYS = frozenset({"only", "order", "ignore", "quantizations"})
_ROUTING_BOOL_KEYS = frozenset({"allow_fallbacks", "require_parameters"})
_ROUTING_STR_KEYS = frozenset({"sort", "data_collection"})


def _clean_provider_routing(raw: dict[str, Any]) -> dict[str, Any]:
    known = _ROUTING_LIST_KEYS | _ROUTING_BOOL_KEYS | _ROUTING_STR_KEYS
    unknown = sorted(set(raw) - known)
    if unknown:
        raise BadRequest(
            f"Неизвестные ключи маршрутизации: {', '.join(unknown)}; допустимые: "
            f"{', '.join(sorted(known))}"
        )
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _ROUTING_LIST_KEYS:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise BadRequest(f"`{key}` должен быть списком строк")
            items = list(dict.fromkeys(v.strip() for v in value if v.strip()))
            if items:
                cleaned[key] = items
        elif key in _ROUTING_BOOL_KEYS:
            if not isinstance(value, bool):
                raise BadRequest(f"`{key}` должен быть true или false")
            cleaned[key] = value
        else:
            if not isinstance(value, str) or not value.strip():
                raise BadRequest(f"`{key}` должен быть непустой строкой")
            cleaned[key] = value.strip()
    return cleaned


def parse_fallback(value: Any) -> FallbackPolicy | None:
    """A bad policy name is the caller's mistake, so answer 400, not 502."""
    try:
        return FallbackPolicy.parse(value)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def _channel(http_request: Request | None) -> tuple[str, str | None]:
    """Where this call came from, for the log.

    The console sends `X-Router-Channel: ui`; anything else calling `/api` is a
    program. The client label is free-form and untrusted — a grouping key in the
    log, exactly like `external_user_id`.
    """
    if http_request is None:
        return "api", None
    channel = (http_request.headers.get("x-router-channel") or "api").strip().lower()
    if channel not in ("ui", "api", "mcp"):
        channel = "api"
    client = http_request.headers.get("x-router-client")
    return channel, (client or None)


async def _run(request: RouterRequest) -> dict[str, Any]:
    """Dispatch and answer with the Run envelope.

    RouterError travels up to the app-level handler, which turns it into the
    error envelope with its code, hint and `log_id`; catching it here would throw
    away exactly the fields the console needs.
    """
    result = await get_core().router.dispatch(request)
    return run_from_dispatch(result, request)


class ChatBody(BaseModel):
    external_user_id: str = "web-tester"
    text: str | None = None
    messages: list[dict[str, Any]] | None = None
    model_id: str | None = None
    tier: str | None = None
    provider: str | None = None
    params: dict[str, Any] | None = None
    # {"policy": "same"|"cheaper"|"pricier", "max_steps": 1, "scope": ...}
    # or just the policy name. See FallbackPolicy.
    fallback: dict[str, Any] | str | None = None
    # Which application is asking: the site it runs on wins, a plain name is
    # the fallback. Logged, filterable, and forwarded to providers that take
    # such a parameter; omitted, the request goes out as the router itself.
    app_url: str | None = None
    app_name: str | None = None


class DecideBody(BaseModel):
    """Typed questions about a text (`text-to-decisions`, TypeSafe Jev).

    `state` is what the model judges: a string, or a JSON object/array of
    related context; `text` is accepted as an alias for a string state.
    `questions` is name → {type: noul|choice|score, instructions, criteria}.
    """

    external_user_id: str = "web-tester"
    questions: dict[str, Any]
    state: str | dict[str, Any] | list[Any] | None = None
    text: str | None = None
    model_id: str | None = None
    provider: str | None = None
    fallback: dict[str, Any] | str | None = None
    app_url: str | None = None
    app_name: str | None = None


class ImageBody(BaseModel):
    external_user_id: str = "web-tester"
    prompt: str
    model_id: str | None = None
    provider: str | None = None
    params: dict[str, Any] | None = None
    fallback: dict[str, Any] | str | None = None
    app_url: str | None = None
    app_name: str | None = None


class TtsBody(BaseModel):
    external_user_id: str = "web-tester"
    text: str
    voice: str | None = None
    model_id: str | None = None
    provider: str | None = None
    # `voice` above is the shorthand the console has always sent; `params` is the
    # general channel. Without it `language` was reachable from the adapter and
    # from MCP but not from the web API, so the form described a field that did
    # not exist.
    params: dict[str, Any] | None = None
    fallback: dict[str, Any] | str | None = None
    app_url: str | None = None
    app_name: str | None = None


class CliBody(BaseModel):
    external_user_id: str = "web-tester"
    prompt: str
    engine: str = "codex"  # "codex" | "claude"
    cwd: str | None = None
    timeout_sec: int | None = None
    # Same gap as tts: `model` (claude-cli) had no way in from the web API.
    params: dict[str, Any] | None = None
    app_url: str | None = None
    app_name: str | None = None


class ManualModelBody(BaseModel):
    service_provider_id: str
    model_id: str
    display_name: str = ""
    model_provider: str | None = None
    # What the model can be asked to do. `modalities` is the older field and is
    # used only when `tasks` is absent; the groups are derived either way.
    tasks: list[str] = []
    modalities: list[str] = ["chat"]
    capabilities: list[str] = []
    tier: str | None = None
    prompt_price: float = 0.0
    completion_price: float = 0.0
    image_price: float = 0.0
    audio_price_per_second: float = 0.0
    context_length: int = 0
    pricing_mode: str = "per_request"
    subscription_id: int | None = None
    # Where the price came from. Defaults to "manual" (a human typed it in);
    # pass the vendor's price page in `price_source` so the number stays traceable.
    price_source: str | None = None
    price_source_kind: str = "manual"
    # The model's caveats, shown wherever a human picks a model.
    description: str | None = None


class ProviderPatch(BaseModel):
    enabled: bool


@api_router.get("/capabilities")
async def api_capabilities() -> dict[str, Any]:
    """What this installation can do: the parameter matrix, the providers, what
    the server fixes, and the glossary. The console draws its forms from here
    rather than from a copy of the matrix maintained by hand."""
    core = get_core()
    return capabilities.build(
        core.settings,
        core.adapters,
        await core.db.get_service_providers(),
        await core.db.get_enabled_provider_ids(),
    )


class PlanBody(BaseModel):
    # `task` is the field; `modality` is what callers written before ADR-0006
    # send, and it still resolves (`chat` means `text-to-text`).
    task: str | None = None
    modality: str = "chat"
    external_user_id: str = "web-tester"
    text: str | None = None
    messages: list[dict[str, Any]] | None = None
    model_id: str | None = None
    tier: str | None = None
    provider: str | None = None
    params: dict[str, Any] | None = None
    fallback: dict[str, Any] | str | None = None


@api_router.post("/plan")
async def api_plan(body: PlanBody) -> dict[str, Any]:
    """The dry run. Nothing is sent, nothing is spent, nothing is logged.

    Answers 200 even when no model can serve the request: "почему запрос не
    ушёл" is the case this screen exists for, and an error status would leave
    the console with nothing to render.
    """
    try:
        task = resolve_task(body.task, fallback=body.modality)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    request = RouterRequest(
        task=task,
        external_user_id=body.external_user_id,
        model_id=body.model_id,
        tier=body.tier,
        provider_hint=body.provider,
        text=body.text,
        messages=body.messages,
        params=body.params or {},
        fallback=parse_fallback(body.fallback),
    )
    core = get_core()
    plan = await core.router.explain(request)
    plan["warnings"] = _plan_warnings(core, task, plan)
    return plan


def _plan_warnings(core, task: Task, plan: dict[str, Any]) -> list[str]:
    """What would stop this request that the ladder cannot see.

    The dry run is supposed to answer «сработает ли», and a clean plan for a task
    whose files cannot be delivered is a confident wrong answer. Scoped to the
    provider that would actually be called, and only to tasks that carry a file
    at all — a text prompt needs none of this.
    """
    signature = task.signature
    if not ({Media.IMAGE, Media.AUDIO, Media.VIDEO, Media.FILE}
            & (signature.requires | signature.optional)):
        return []
    provider_id = (plan.get("will_call") or {}).get("provider")
    adapter = core.adapters.get(provider_id) if provider_id else None
    if adapter is None:
        return []
    warning = public_blobs.provider_warning(core.settings, adapter)
    return [warning] if warning else []


@api_router.get("/health/summary")
async def api_health_summary() -> dict[str, Any]:
    """The state of the installation, behind the auth gate.

    Public `/health` answers only that the service is alive; version, provider
    inventory and disk usage are a map of the host and live here (§16.5).
    """
    core = get_core()
    return await health.summary(core, core.started_at)


@api_router.post("/providers/{provider_id}/probe")
async def api_probe_provider(provider_id: str, force: bool = False) -> dict[str, Any]:
    """Ask a provider whether the key we hold still works.

    `configured` only checks that a key is present, so a revoked key reads as
    healthy. The answer is cached — probing on every page load would spend the
    rate limit on reassurance.
    """
    result = await health.probe_provider(get_core(), provider_id, force=force)
    return result.as_dict()


@api_router.get("/mcp/summary")
async def api_mcp_summary(hours: int = 24) -> dict[str, Any]:
    """Tool inventory with schemas, joined with what the log saw."""
    return await mcp_summary.summary(get_core(), hours=max(1, min(hours, 24 * 30)))


@api_router.post("/mcp/probe")
async def api_mcp_probe() -> dict[str, Any]:
    return await mcp_summary.probe()


@api_router.get("/models")
async def api_models(
    task: str | None = None,
    modality: str | None = None,
    tier: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    core = get_core()
    models = await core.registry.get_models(
        task=task, modality=modality, tier=tier, provider=provider
    )
    items = [model_to_dict(m) for m in models]
    return page(items, total=len(items))


@api_router.post("/chat")
async def api_chat(body: ChatBody, http_request: Request) -> dict[str, Any]:
    channel, client_id = _channel(http_request)
    return await _run(
        RouterRequest(
            task=Task.TEXT_TO_TEXT,
            external_user_id=body.external_user_id,
            model_id=body.model_id,
            tier=body.tier,
            provider_hint=body.provider,
            text=body.text,
            messages=body.messages,
            params=body.params or {},
            fallback=parse_fallback(body.fallback),
            channel=channel,
            client_id=client_id,
            app_url=body.app_url,
            app_name=body.app_name,
        )
    )


def _decision_inputs(
    state: str | dict[str, Any] | list[Any] | None,
    text: str | None,
    params: dict[str, Any],
) -> str:
    """Fold a decisions request's state into (`text`, `params["state"]`).

    A string state is the prompt; a structured one travels to the provider in
    `params["state"]` and is written to the log as its JSON text, so the
    journal shows what was judged either way.
    """
    if state is None or state == "":
        state = text
    if state is None or state == "":
        raise BadRequest("Provide the text to judge in `state` (or `text`)")
    if isinstance(state, str):
        return state
    params["state"] = state
    return json.dumps(state, ensure_ascii=False)


@api_router.post("/decide")
async def api_decide(body: DecideBody, http_request: Request) -> dict[str, Any]:
    channel, client_id = _channel(http_request)
    params: dict[str, Any] = {"questions": body.questions}
    prompt = _decision_inputs(body.state, body.text, params)
    request = RouterRequest(
        task=Task.TEXT_TO_DECISIONS,
        external_user_id=body.external_user_id,
        model_id=body.model_id,
        provider_hint=body.provider,
        text=prompt,
        params=params,
        fallback=parse_fallback(body.fallback),
        channel=channel,
        client_id=client_id,
        app_url=body.app_url,
        app_name=body.app_name,
    )
    result = await get_core().router.dispatch(request)
    return run_from_dispatch(result, request) | {
        # The Run envelope carries the answers as `text`; the parsed shape is
        # what an application actually reads.
        "answers": result.response.raw_meta.get("answers"),
        "served_model_id": result.response.raw_meta.get("served_model_id"),
    }


@api_router.post("/image")
async def api_image(body: ImageBody, http_request: Request) -> dict[str, Any]:
    channel, client_id = _channel(http_request)
    return await _run(
        RouterRequest(
            task=Task.TEXT_TO_IMAGE,
            external_user_id=body.external_user_id,
            model_id=body.model_id,
            provider_hint=body.provider,
            text=body.prompt,
            params=body.params or {},
            fallback=parse_fallback(body.fallback),
            channel=channel,
            client_id=client_id,
            app_url=body.app_url,
            app_name=body.app_name,
        )
    )


@api_router.post("/transcribe")
async def api_transcribe(
    http_request: Request,
    file: UploadFile = File(...),
    external_user_id: str = Form("web-tester"),
    model_id: str | None = Form(None),
    provider: str | None = Form(None),
    app_url: str | None = Form(None),
    app_name: str | None = Form(None),
) -> dict[str, Any]:
    audio = await file.read()
    channel, client_id = _channel(http_request)
    return await _run(
        RouterRequest(
            task=Task.SPEECH_TO_TEXT,
            external_user_id=external_user_id,
            model_id=model_id,
            provider_hint=provider,
            attachments=[
                Attachment(kind="audio", mime=file.content_type or "audio/ogg", data=audio)
            ],
            channel=channel,
            client_id=client_id,
            app_url=app_url,
            app_name=app_name,
        )
    )


@api_router.post("/tts")
async def api_tts(body: TtsBody, http_request: Request) -> dict[str, Any]:
    params: dict[str, Any] = dict(body.params or {})
    if body.voice:
        params["voice"] = body.voice
    channel, client_id = _channel(http_request)
    return await _run(
        RouterRequest(
            task=Task.TEXT_TO_SPEECH,
            external_user_id=body.external_user_id,
            model_id=body.model_id,
            provider_hint=body.provider,
            text=body.text,
            params=params,
            fallback=parse_fallback(body.fallback),
            channel=channel,
            client_id=client_id,
            app_url=body.app_url,
            app_name=body.app_name,
        )
    )


@api_router.post("/cli")
async def api_cli(body: CliBody, http_request: Request) -> dict[str, Any]:
    engine = body.engine.strip().lower()
    if engine not in ("codex", "claude"):
        raise BadRequest("engine must be 'codex' or 'claude'")
    params: dict[str, Any] = dict(body.params or {})
    if body.cwd:
        params["cwd"] = body.cwd
    if body.timeout_sec:
        params["timeout_sec"] = body.timeout_sec
    channel, client_id = _channel(http_request)
    return await _run(
        RouterRequest(
            task=Task.CLI,
            external_user_id=body.external_user_id,
            provider_hint=f"{engine}-cli",
            text=body.prompt,
            params=params,
            channel=channel,
            client_id=client_id,
            app_url=body.app_url,
            app_name=body.app_name,
        )
    )


# --- runs in flight -----------------------------------------------------
class Attached(BaseModel):
    """One uploaded file, by reference. `kind` is `image` | `audio` | `file`."""

    ref: str
    kind: str = "file"
    # What the file is *for*, when the model takes more than one of a kind: the
    # mask, the garment, the face. Omitted means the obvious one, bound in the
    # order the list gives — which is all a single-image model ever needed.
    role: str | None = None


class RunBody(BaseModel):
    """One run of any task, submitted without waiting for it.

    The fields are the union of the five synchronous bodies rather than a
    per-task endpoint set: the console builds this object from the capability
    matrix, and a second dispatch tree here would be a second place to forget a
    parameter.
    """

    task: str | None = None
    modality: str = "chat"
    external_user_id: str = "web-tester"
    text: str | None = None
    prompt: str | None = None          # what the image and cli forms call it
    messages: list[dict[str, Any]] | None = None
    model_id: str | None = None
    tier: str | None = None
    provider: str | None = None
    voice: str | None = None           # tts shorthand, as on /api/tts
    # decisions shorthand, as on /api/decide: the console sends both inside
    # `params`, an API caller may name them at the top level.
    questions: dict[str, Any] | None = None
    state: str | dict[str, Any] | list[Any] | None = None
    engine: str | None = None          # cli: "codex" | "claude"
    cwd: str | None = None
    timeout_sec: int | None = None
    params: dict[str, Any] | None = None
    fallback: dict[str, Any] | str | None = None
    # Uploaded files, by reference. Attachments do not travel through JSON:
    # POST /api/v1/blobs first, send the refs here. Editing an image with a mask
    # needs two, and asking about two photographs needs two, so the list is the
    # field; `attachment_ref` stays for callers that predate it and is folded in.
    attachments: list[Attached] | None = None
    attachment_ref: str | None = None
    attachment_kind: str = "audio"
    # Idempotency and grouping. The same key twice returns the first run instead
    # of spending the money again; the batch groups one operator action.
    client_request_id: str | None = None
    batch_id: str | None = None
    # The application making the request: the site it runs on, else a name.
    app_url: str | None = None
    app_name: str | None = None


def _attachment_from_ref(ref: str, kind: str, role: str | None = None) -> Attachment:
    core = get_core()
    if not core.blobs.exists(ref):
        raise NotFound(f"Файл {ref} не найден")
    if role is not None and role not in ATTACHMENT_ROLES:
        # A typo'd role would otherwise reach the adapter and fail there with a
        # provider's name on it, for a mistake the caller made in this request.
        raise BadRequest(
            f"Неизвестная роль вложения {role!r}; допустимые: "
            f"{', '.join(ATTACHMENT_ROLES)}"
        )
    return Attachment(
        kind=kind,
        mime=core.blobs.mime_of(ref),
        data=core.blobs.load(ref),
        ref=ref,
        role=role,
    )


def _router_request(body: RunBody, http_request: Request | None) -> RouterRequest:
    try:
        task = resolve_task(body.task, fallback=body.modality)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    channel, client_id = _channel(http_request)

    params: dict[str, Any] = dict(body.params or {})
    provider = body.provider
    if task is Task.TEXT_TO_SPEECH and body.voice:
        params["voice"] = body.voice
    text = body.text if body.text is not None else body.prompt
    if task is Task.TEXT_TO_DECISIONS:
        if body.questions is not None:
            params["questions"] = body.questions
        state = body.state if body.state is not None else params.get("state")
        if state is not None or text:
            params.pop("state", None)
            text = _decision_inputs(state, text, params)
    if task is Task.CLI:
        engine = (body.engine or "codex").strip().lower()
        if engine not in ("codex", "claude"):
            raise BadRequest("engine must be 'codex' or 'claude'")
        provider = f"{engine}-cli"
        if body.cwd:
            params["cwd"] = body.cwd
        if body.timeout_sec:
            params["timeout_sec"] = body.timeout_sec

    refs: list[tuple[str, str, str | None]] = [
        (a.ref, a.kind, a.role) for a in body.attachments or []
    ]
    if body.attachment_ref and body.attachment_ref not in {r for r, _, _ in refs}:
        refs.append((body.attachment_ref, body.attachment_kind, None))
    attachments = [_attachment_from_ref(ref, kind, role) for ref, kind, role in refs]

    # One check for every task, from the task's own signature. The old hard-coded
    # "распознавание needs a file" left image-to-image with no message at all.
    try:
        validate_task_inputs(
            task, has_text=bool(text or body.messages), media=media_of(attachments)
        )
    except ValueError as exc:
        raise BadRequest(
            f"{exc}. Файлы передаются ссылкой: сначала POST /api/v1/blobs"
        ) from exc

    return RouterRequest(
        task=task,
        external_user_id=body.external_user_id,
        model_id=body.model_id,
        tier=body.tier,
        provider_hint=provider,
        text=text,
        messages=body.messages,
        attachments=attachments,
        params=params,
        fallback=parse_fallback(body.fallback),
        channel=channel,
        client_id=client_id,
        client_request_id=body.client_request_id,
        batch_id=body.batch_id,
        app_url=body.app_url,
        app_name=body.app_name,
    )


@api_router.post("/runs", status_code=202)
async def api_submit_run(body: RunBody, http_request: Request) -> dict[str, Any]:
    """Start a run and answer with its id, without waiting for it.

    202, not 200: nothing has happened yet beyond accepting the work. The
    console polls `GET /runs/{id}` once a second (§10.5); an operator who closes
    the tab loses nothing, because the row in the log was written at the start.
    """
    request = _router_request(body, http_request)
    registry = get_core().runs
    if registry is None:  # pragma: no cover - always wired in build_core
        raise Conflict("Реестр прогонов недоступен")
    run = await registry.submit(
        request, label=body.model_id or request.provider_hint or request.task.value
    )
    return run.as_dict()


@api_router.get("/runs")
async def api_list_runs(batch_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    """What this process is running now, newest first.

    In-process only, on purpose: this is the лента последних прогонов, not the
    log. Anything older lives in `GET /logs`, which survives restarts.
    """
    registry = get_core().runs
    runs = registry.list(batch_id=batch_id, limit=max(1, min(limit, 100)))
    return page([r.as_dict() for r in runs], total=len(runs))


@api_router.get("/runs/{run_id}")
async def api_get_run(run_id: str) -> dict[str, Any]:
    return get_core().runs.get(run_id).as_dict()


@api_router.delete("/runs/{run_id}")
async def api_cancel_run(run_id: str) -> dict[str, Any]:
    """Stop a run. For a CLI engine this kills the process; for an HTTP call it
    cancels the task. Either way the log row is closed as `cancelled` with the
    cost marked unknown — the provider may already have done the work."""
    run = await get_core().runs.cancel(run_id)
    return run.as_dict()


@api_router.post("/blobs")
async def api_upload_blob(file: UploadFile = File(...)) -> dict[str, Any]:
    """Store a file and answer with its ref, so JSON bodies can point at it.

    The audio for a transcription used to be reachable only through a multipart
    form, which meant the asynchronous run path could not carry one at all.
    """
    core = get_core()
    data = await file.read()
    if not data:
        raise BadRequest("Пустой файл")
    ref = core.blobs.save(data, file.content_type or "application/octet-stream")
    return {
        "ref": ref,
        "bytes": len(data),
        "mime": core.blobs.mime_of(ref),
        "url": f"/blobs/{ref}",
    }


def log_filters(
    user: str | None = None,
    status: str | None = None,
    modality: str | None = None,
    task: str | None = None,
    model_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    app: str | None = None,
    channel: str | None = None,
    batch_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    with_fallback: bool = False,
    with_retries: bool = False,
    unverified_cost: bool = False,
    cost_mismatch: bool = False,
) -> dict[str, Any]:
    """The log's filter set as query parameters, in the names the storage
    layer uses. One dependency, so the page and a row's neighbours cannot
    disagree about what «the same filters» means.

    `user`, `model` and `app` match by substring — the console's boxes are
    search fields, and «ort-bot» has to find every ort-bot-XXXX. `model_id`
    stays the exact match for callers that hold a full id.
    """
    return dict(
        user_contains=user,
        status=status,
        modality=modality,
        task=task,
        model_id=model_id,
        model_contains=model,
        provider=provider,
        app_contains=app,
        channel=channel,
        batch_id=batch_id,
        since=since,
        until=until,
        with_fallback=with_fallback,
        with_retries=with_retries,
        unverified_cost=unverified_cost,
        cost_mismatch=cost_mismatch,
    )


@api_router.get("/logs")
async def api_logs(
    filters: dict[str, Any] = Depends(log_filters),
    cursor: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """One page of the log, newest first.

    `cursor` is the id to page *before*: the log grows from the head, so an
    offset would show part of page one again on page two.
    """
    core = get_core()
    limit = max(1, min(limit, 200))
    rows = await core.db.list_request_logs(before_id=cursor, limit=limit, **filters)
    total = await core.db.count_request_logs(**filters)
    next_cursor = rows[-1]["id"] if len(rows) == limit else None
    return page(rows, total=total, next_cursor=next_cursor)


@api_router.get("/logs/images")
async def api_log_images(
    filters: dict[str, Any] = Depends(log_filters),
    cursor: int | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    """Rows that carried a picture, newest first, one card per row.

    Takes the journal's filter set, so the gallery narrows exactly the way the
    table does (the money and routing flags are accepted and simply select
    nothing interesting). Declared before `/logs/{log_id}` for the same reason
    as `/logs/suggest`. `total` is None: counting means scanning every stored
    request snapshot for a figure the gallery does not show. The cursor is the
    last row's id.
    """
    core = get_core()
    limit = max(1, min(limit, 100))
    rows = await core.db.list_image_log_rows(before_id=cursor, limit=limit, **filters)
    # The SQL predicate is a coarse net (a mime in the snapshot, a suffix on a
    # ref); the card decides what is actually showable, and a row that turns
    # out to hold nothing showable is not a card.
    items = [card for card in (image_card(row) for row in rows) if card["images"]]
    next_cursor = rows[-1]["id"] if len(rows) == limit else None
    return page(items, total=None, next_cursor=next_cursor)


@api_router.get("/logs/suggest")
async def api_logs_suggest(field: str, q: str = "", limit: int = 20) -> dict[str, Any]:
    """Values a log filter can take, freshest first.

    Declared before `/logs/{log_id}` so the path wins over the int-typed
    parameter. The list comes from the log itself, not the registry: the filter
    must offer exactly the users, models and providers the journal contains.
    """
    try:
        items = await get_core().db.suggest_log_values(
            field, contains=q.strip(), limit=max(1, min(limit, 50))
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return page(items, total=len(items))


@api_router.get("/logs/{log_id}")
async def api_log(log_id: int) -> dict[str, Any]:
    """The full row as a Run envelope — the same shape the inspector renders
    right after a run, so one component serves both."""
    row = await get_core().db.get_request_log(log_id)
    if row is None:
        raise NotFound(f"No log entry {log_id}")
    return run_from_log(row)


@api_router.get("/logs/{log_id}/neighbors")
async def api_log_neighbors(
    log_id: int, filters: dict[str, Any] = Depends(log_filters)
) -> dict[str, Any]:
    """The rows on either side of one entry under the page's filters — what
    the entry window's «вверх» and «вниз» step to. `newer_id` is the row above
    in the table (a higher id), `older_id` the row below; None means the list
    ends there."""
    return await get_core().db.neighbor_log_ids(log_id, **filters)


@api_router.get("/retention")
async def api_retention() -> dict[str, Any]:
    """The policy and what applying it would remove. Deletes nothing."""
    return await retention.preview(get_core())


@api_router.post("/retention/purge")
async def api_retention_purge(orphans: bool = True) -> dict[str, Any]:
    """Apply the retention policy now.

    Irreversible, and the console asks first with these numbers in the prompt:
    the log is the only record of what was spent, and no export runs behind the
    operator's back to soften a deletion they did not mean.
    """
    return await retention.purge(get_core(), orphans_too=orphans)


@api_router.get("/notifications")
async def api_notifications() -> dict[str, Any]:
    """Which channels are on, what is muted, and what became of recent failures."""
    notifier = get_core().notifier
    if notifier is None:  # pragma: no cover - bootstrap always builds one
        return {"enabled": False, "channels": [], "muted_codes": [], "recent": []}
    return notifier.describe()


@api_router.post("/notifications/test")
async def api_notifications_test() -> dict[str, Any]:
    """Send a test message to every channel and say how each one answered.

    Answers 200 even when a channel failed: the point of the button is to see
    the failure text next to the channel name, not a bare 502.
    """
    notifier = get_core().notifier
    if notifier is None:  # pragma: no cover
        raise NotConfigured("Оповещения не собраны")
    results = await notifier.send_test()
    return {"results": results, "ok": all(r["status"] == "ok" for r in results)}


@api_router.get("/usage")
async def api_usage(
    group_by: str = "user",
    user: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Spend, grouped, with the totals split by how much each number is worth.

    Confirmed rates, unconfirmed rates, notional subscription money and the
    requests nobody can price are four different facts; one total hides all
    four, and `is_lower_bound` says when the sum is only a floor (§7.2).
    """
    core = get_core()
    try:
        items, totals = await core.db.get_spend(
            group_by=group_by, external_user_id=user, since=since, until=until
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    # Per-day series for the sparkline, independent of the chosen grouping.
    daily, _ = await core.db.get_spend(
        group_by="day", external_user_id=user, since=since, until=until
    )
    daily.sort(key=lambda r: r["bucket"])
    return page(
        items,
        total=len(items),
        facets={
            "totals": totals,
            "group_by": group_by,
            "groupings": sorted(core.db.USAGE_GROUPINGS),
            "daily": [
                {"day": r["bucket"], "requests": r["requests"], "usd": r["total_usd"]}
                for r in daily
            ],
        },
    )


@api_router.get("/usage/export")
async def api_usage_export(
    group_by: str = "user",
    user: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> Response:
    """The same numbers as CSV. Columns stay split by price source: collapsing
    them into one total in an export would launder the distinction the screen
    works to keep."""
    core = get_core()
    try:
        items, _totals = await core.db.get_spend(
            group_by=group_by, external_user_id=user, since=since, until=until
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    columns = [
        "bucket", "requests", "total_tokens", "prompt_tokens", "completion_tokens",
        "cache_read_tokens", "reasoning_tokens", "confirmed_usd", "unconfirmed_usd",
        "notional_usd", "unpriced_requests", "unknown_requests", "errors",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(items)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="usage-{group_by}.csv"'},
    )


# --- registry admin -----------------------------------------------------
async def _provider_pricing_url(core, provider_id: str) -> str | None:
    """Fall back to the provider's price page when a manual row names no source."""
    for p in await core.db.get_service_providers():
        if p.id == provider_id:
            return p.pricing_url
    return None


@api_router.get("/registry/providers")
async def api_providers() -> dict[str, Any]:
    core = get_core()
    providers = await core.db.get_service_providers()
    items = [
        {
            "id": p.id,
            "display_name": p.display_name,
            "adapter": p.adapter,
            "enabled": p.enabled,
            # Live adapter state: `enabled` is the admin toggle and survives
            # restarts even when the adapter has lost its key/binary.
            "configured": p.id in core.adapters and core.adapters[p.id].is_configured(),
            "base_url": p.base_url,
            "pricing_url": p.pricing_url,
            # By name only: the console must never learn a secret's value.
            "env_var": core.adapters[p.id].env_var if p.id in core.adapters else None,
        }
        for p in providers
    ]
    return page(items, total=len(items))


@api_router.patch("/registry/providers/{provider_id}")
async def api_patch_provider(provider_id: str, body: ProviderPatch) -> dict[str, Any]:
    core = get_core()
    await core.db.set_provider_enabled(provider_id, body.enabled)
    return {"id": provider_id, "enabled": body.enabled}


@api_router.post("/registry/refresh")
async def api_refresh_catalog(provider: str | None = None) -> dict[str, Any]:
    """Re-read provider catalogs now. Without `provider`, every provider that
    has one. Reports what changed, including models deprecated upstream."""
    core = get_core()
    reports = (
        [await core.registry.sync_provider(provider)]
        if provider
        else await core.registry.sync_all(force=True)
    )
    return {"providers": [r.as_dict() for r in reports]}


@api_router.get("/registry/catalog/{provider_id}")
async def api_browse_catalog(provider_id: str) -> dict[str, Any]:
    """What the provider offers right now. Nothing is stored by this call.

    The registry keeps the models that were used and answered; the full list is a
    question, not a record, so it is asked when somebody asks it (ADR-0008).
    """
    return await get_core().registry.browse_provider(provider_id)


class AdoptBody(BaseModel):
    model_ids: list[str]


@api_router.post("/registry/catalog/{provider_id}/adopt")
async def api_adopt_models(provider_id: str, body: AdoptBody) -> dict[str, Any]:
    """Add named models from the live catalog to the registry."""
    if not body.model_ids:
        raise BadRequest("No models given")
    if len(body.model_ids) > 200:
        raise BadRequest("Too many models in one action (max 200)")
    adopted = await get_core().registry.adopt_models(provider_id, body.model_ids)
    return {"provider_id": provider_id, "adopted": adopted, "count": len(adopted)}


@api_router.post("/registry/catalog/{provider_id}/prune")
async def api_prune_catalog(provider_id: str, dry_run: bool = True) -> dict[str, Any]:
    """Retire the rows nothing ever confirmed. `dry_run=true` only counts them."""
    report = await get_core().registry.prune_unconfirmed(provider_id, dry_run=dry_run)
    return report.as_dict()


def _facets(models: list[RegistryModel]) -> dict[str, Any]:
    """Counts for the sidebar, computed over the same list that was returned.

    Counting in a second call would show numbers from a different moment than
    the rows beside them, and a sidebar that disagrees with its table is worse
    than no sidebar (§11).
    """
    def _tally(key) -> dict[str, int]:
        counts: dict[str, int] = {}
        for model in models:
            for value in key(model):
                counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    return {
        "task": _tally(lambda m: [t.value for t in m.tasks]),
        "modality": _tally(lambda m: [mod.value for mod in m.modalities]),
        "provider": _tally(lambda m: [m.service_provider_id]),
        "source": _tally(lambda m: [m.source]),
        "price_source_kind": _tally(lambda m: [m.price_source_kind]),
        "tier": _tally(lambda m: [m.tier]),
        "state": _tally(lambda m: ["active" if m.is_active else "retired"]),
    }


@api_router.get("/registry/models")
async def api_registry_models(
    source: str | None = None,
    modality: str | None = None,
    task: str | None = None,
    provider: str | None = None,
    tier: str | None = None,
    state: str | None = None,
    price_source_kind: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    core = get_core()
    models = await core.db.list_registry_models(
        source=source,
        modality=modality,
        task=task,
        service_provider_id=provider,
        tier=tier,
        active_only=False,
    )
    if state == "active":
        models = [m for m in models if m.is_active]
    elif state == "retired":
        models = [m for m in models if not m.is_active]
    if price_source_kind:
        models = [m for m in models if m.price_source_kind == price_source_kind]
    if search:
        needle = search.strip().lower()
        models = [
            m for m in models
            if needle in m.model_id.lower() or needle in (m.display_name or "").lower()
        ]
    items = [model_to_dict(m) for m in models]
    return page(items, total=len(items), facets=_facets(models))


class BulkModelsBody(BaseModel):
    """A mass action, addressed by explicit pairs.

    Pairs rather than a filter on purpose: a filter re-evaluated server-side can
    match rows the operator never saw, and the confirmation they agreed to
    listed the ones they did.
    """

    models: list[dict[str, str]]           # [{service_provider_id, model_id}]
    action: str                            # "deprecate" | "restore" | "set_price_kind"
    price_source_kind: str | None = None


@api_router.post("/registry/models/bulk")
async def api_bulk_models(body: BulkModelsBody) -> dict[str, Any]:
    core = get_core()
    if body.action not in ("deprecate", "restore", "set_price_kind"):
        raise BadRequest(f"Unknown action {body.action!r}")
    if not body.models:
        raise BadRequest("No models given")
    if len(body.models) > 500:
        raise BadRequest("Too many models in one action (max 500)")

    changed: list[dict[str, str]] = []
    for pair in body.models:
        provider = pair.get("service_provider_id")
        model_id = pair.get("model_id")
        if not provider or not model_id:
            raise BadRequest("Each entry needs service_provider_id and model_id")
        if body.action == "deprecate":
            # Deprecate, never delete: logs and cost history point at the row.
            await core.db.deprecate_registry_model(provider, model_id)
        elif body.action == "restore":
            # The undo behind the "отменить" window. If the provider really did
            # retire the model, the next sync deprecates it again — the registry
            # follows the provider, not the button.
            await core.db.restore_registry_model(provider, model_id)
        else:
            if not body.price_source_kind:
                raise BadRequest("set_price_kind needs price_source_kind")
            await core.db.update_registry_model(
                provider, model_id, {"price_source_kind": body.price_source_kind}
            )
        changed.append({"service_provider_id": provider, "model_id": model_id})
    return {"action": body.action, "changed": changed, "count": len(changed)}


def _model_param_options(model: RegistryModel) -> dict[str, list[str]]:
    """Per-model option lists, read off the capabilities the catalog published.

    Today that is the reasoning levels: the Codex catalog names them per model
    (gpt-5.6 accepts `ultra`, gpt-5.4 stops at `xhigh`), and the console used to
    offer a fixed low/medium/high to every model that reads the parameter.
    """
    efforts = [c.split(":", 1)[1] for c in model.capabilities if c.startswith("effort:")]
    return {"reasoning_effort": efforts} if efforts else {}


@api_router.get("/registry/tree")
async def api_registry_tree(
    task: str | None = None, modality: str | None = None
) -> dict[str, Any]:
    """The cascade the picker needs: provider → vendor → model → variant.

    Built here because the identifier split is domain knowledge. The client used
    to cut `model_id` on `/` and `:`, guessing at a structure the database
    already stores — `model_provider` is its own column.
    """
    core = get_core()
    models = await core.db.list_registry_models(
        task=task, modality=modality, active_only=True
    )
    enabled = await core.db.get_enabled_provider_ids()

    providers: dict[str, dict[str, Any]] = {}
    for m in models:
        base, _, variant = m.model_id.partition(":")
        base = base.split("/")[-1]
        provider = providers.setdefault(
            m.service_provider_id,
            {
                "value": m.service_provider_id,
                "count": 0,
                "enabled": m.service_provider_id in enabled,
                "configured": (
                    m.service_provider_id in core.adapters
                    and core.adapters[m.service_provider_id].is_configured()
                ),
                "children": {},
            },
        )
        vendor = provider["children"].setdefault(
            m.model_provider, {"value": m.model_provider, "count": 0, "children": {}}
        )
        family = vendor["children"].setdefault(
            base, {"value": base, "count": 0, "children": []}
        )
        family["children"].append(
            {
                "value": m.model_id,
                # What this model in particular accepts, when its catalog says so.
                # The form-level list is the union across a provider's models, and
                # a model rejects a level it does not know with a 400 — so a
                # pinned model narrows the select to its own levels.
                "param_options": _model_param_options(m),
                # Knobs only this model has. The pair-level matrix cannot carry
                # them — that is the whole point of a per-model allowlist — so
                # the picker hands them to the form when the model is pinned.
                "extra_params": list(m.extra_params),
                # The roles this model actually binds. Offering exactly these
                # beats offering all six: a try-on model has no use for a mask,
                # and a list of everything makes the operator guess.
                "roles": sorted(m.input_map),
                # The variant carries its price, because "cheap" as a word tells
                # an operator nothing they can act on.
                "label": f"{variant or 'базовая'} · ${(m.prompt_price + m.completion_price) * 1e6:.3f}/1M",
                "tier": m.tier,
                "price_source_kind": m.price_source_kind,
            }
        )
        provider["count"] += 1
        vendor["count"] += 1
        family["count"] += 1

    def _sorted(node: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for value in sorted(node.values(), key=lambda n: n["value"]):
            children = value["children"]
            if isinstance(children, dict):
                value = {**value, "children": _sorted(children)}
            else:
                value = {**value, "children": sorted(children, key=lambda c: c["value"])}
            out.append(value)
        return out

    return {"total": len(models), "children": _sorted(providers)}


class ModelPatch(BaseModel):
    """Partial edit of one registry row, addressed by the pair.

    Every field is optional and only what is sent is written. The old whole-row
    write was the bug: it filled everything the form did not carry with
    defaults, so editing a price wiped the context length and the catalog dates
    and brought a retired model back to life as active.
    """

    prompt_price: float | None = None       # USD per 1M tokens, as shown
    completion_price: float | None = None   # USD per 1M tokens, as shown
    # Also per 1M tokens: a cached prompt token is billed at its own rate, and
    # without it accounting charges cache reads at the prompt rate and marks the
    # whole figure estimated.
    cache_read_price: float | None = None
    cache_write_price: float | None = None
    image_price: float | None = None
    audio_price_per_second: float | None = None
    character_price: float | None = None    # USD per character (TTS)
    # USD per one server-side web search call — per call, not per million:
    # OpenRouter publishes it that way ($0.01 a search on the OpenAI models).
    web_search_price: float | None = None
    # USD per second of the provider's own compute — not per second of audio.
    # This is the only rate most Replicate models have.
    compute_price_per_second: float | None = None
    display_name: str | None = None
    tier: str | None = None
    price_source: str | None = None
    price_source_kind: str | None = None
    pricing_mode: str | None = None
    subscription_id: int | None = None
    # Attachment role -> this model's own input property. `{}` clears it, which
    # is why it is not folded in with the `is not None` fields below: an operator
    # taking a wrong mapping back off a row is a real edit.
    input_map: dict[str, str] | None = None
    # This model's own knobs, on top of the adapter's declared set. `[]` clears
    # it, for the same reason `input_map: {}` does.
    extra_params: list[str] | None = None
    # OpenRouter's `provider` routing object, e.g. `{"only": ["novita/fp8"]}`.
    # `{}` clears it back to OpenRouter's default spread across hosts.
    provider_routing: dict[str, Any] | None = None
    # What the model cannot do, in words. `""` clears it.
    description: str | None = None
    # Peak-hour pricing: `{"windows": ["01:00-04:00"], "multiplier": 2.0,
    # "label": "пиковый тариф"}`, times in UTC. The prices above are then the
    # base (off-peak) rate. `{}` clears it back to a flat round-the-clock rate.
    price_schedule: dict[str, Any] | None = None
    # Optimistic locking: the revision the operator's copy of the row carried.
    revision: int | None = None
    # Drop the local price and take the catalog's again on the next sync.
    revert_price: bool = False


@api_router.patch("/registry/models/{service_provider_id}/{model_id:path}")
async def api_patch_model(
    service_provider_id: str, model_id: str, body: ModelPatch
) -> dict[str, Any]:
    core = get_core()
    model = await core.db.get_registry_model(service_provider_id, model_id)
    if model is None:
        raise NotFound(f"No model {service_provider_id}/{model_id}")

    fields: dict[str, Any] = {}
    if body.revert_price:
        # Hand the row back to the catalog: the next sync overwrites the price
        # because the provenance is no longer "manual".
        fields["price_source_kind"] = "api" if model.source == "catalog" else "estimate"
        fields["price_source"] = None
        fields["price_checked_at"] = None
    else:
        # The form works in USD per million tokens; the registry stores per token.
        for name, per_million in (
            ("prompt_price", body.prompt_price),
            ("completion_price", body.completion_price),
            ("cache_read_price", body.cache_read_price),
            ("cache_write_price", body.cache_write_price),
        ):
            if per_million is not None:
                fields[name] = per_million / 1e6
        for name, value in (
            ("image_price", body.image_price),
            ("audio_price_per_second", body.audio_price_per_second),
            ("character_price", body.character_price),
            ("web_search_price", body.web_search_price),
            ("compute_price_per_second", body.compute_price_per_second),
            ("display_name", body.display_name),
            ("tier", body.tier),
            ("price_source", body.price_source),
            ("pricing_mode", body.pricing_mode),
            ("subscription_id", body.subscription_id),
            ("description", body.description),
        ):
            if value is not None:
                fields[name] = value
        touched_price = any(
            k in fields
            for k in ("prompt_price", "completion_price", "image_price",
                      "audio_price_per_second", "character_price",
                      "cache_read_price", "cache_write_price",
                      "web_search_price", "compute_price_per_second")
        )
        if touched_price:
            # A hand-entered price survives synchronisation, and the criterion is
            # the origin of the *price*, not the origin of the *row*. Keyed on
            # the row, a price typed into a catalog row was wiped by the next
            # sync; keyed on the price, the row stays a catalog row and keeps
            # receiving name, context and task updates.
            fields.setdefault("price_source_kind", body.price_source_kind or "manual")
            fields["price_checked_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
        elif body.price_source_kind is not None:
            fields["price_source_kind"] = body.price_source_kind

    if body.input_map is not None:
        # Outside the price branch: which property the garment lands on has
        # nothing to do with what the model costs, and `revert_price` must not
        # decide whether an operator can fix a binding.
        unknown = sorted(set(body.input_map) - set(ATTACHMENT_ROLES))
        if unknown:
            raise BadRequest(
                f"Неизвестные роли вложений: {', '.join(unknown)}; допустимые: "
                f"{', '.join(ATTACHMENT_ROLES)}"
            )
        cleaned = {k: str(v).strip() for k, v in body.input_map.items() if str(v).strip()}
        fields["input_map"] = json.dumps(cleaned, ensure_ascii=False)

    if body.price_schedule is not None:
        # Inside neither branch: the windows are not a price, so `revert_price`
        # leaves them alone — except that handing the row back to the catalog
        # does clear them, because a catalog that publishes a rate publishes one
        # rate, and a stale multiplier on top of it doubles a number nobody
        # checked. An empty object is how an operator drops the schedule.
        try:
            schedule = PriceSchedule.parse(body.price_schedule)
        except ValueError as exc:
            raise BadRequest(f"Расписание цен: {exc}") from exc
        fields["price_schedule"] = schedule.to_json() if schedule else ""
        if schedule:
            # Typing a schedule is vouching for how the row is priced: the base
            # rate under it is now half of what the vendor advertises, and a sync
            # that overwrote it with the flat catalog figure would leave the
            # multiplier standing on top of the wrong number.
            fields.setdefault("price_source_kind", body.price_source_kind or "manual")
    elif body.revert_price:
        fields["price_schedule"] = ""

    if body.extra_params is not None:
        names = [str(p).strip() for p in body.extra_params]
        bad = sorted({n for n in names if n and not _PARAM_NAME_RE.fullmatch(n)})
        if bad:
            # The column is comma-joined and the name reaches a provider's JSON
            # body, so anything that is not a plain property name is a mistake
            # worth stopping here.
            raise BadRequest(
                f"Не имена параметров: {', '.join(bad)}. Ожидаются имена входов "
                f"модели: латиница, цифры и подчёркивание"
            )
        fields["extra_params"] = ",".join(dict.fromkeys(n for n in names if n))

    if body.provider_routing is not None:
        if body.provider_routing and model.service_provider_id != "openrouter":
            raise BadRequest("Маршрутизация по хостам есть только у OpenRouter")
        fields["provider_routing"] = json.dumps(
            _clean_provider_routing(body.provider_routing), ensure_ascii=False
        )

    if not fields:
        raise BadRequest("Nothing to update")
    updated = await core.db.update_registry_model(
        service_provider_id, model_id, fields, if_revision=body.revision
    )
    if updated is None:
        raise Conflict(
            "Строку изменили, пока она была открыта. Обновите и повторите правку."
        )
    return model_to_dict(updated)


@api_router.post("/registry/models")
async def api_add_manual_model(body: ManualModelBody) -> dict[str, Any]:
    core = get_core()
    try:
        tasks = tuple(Task.from_str(t) for t in body.tasks)
        # Only fall back to the groups when no task was named: given both, the
        # tasks win and the groups are derived from them.
        modalities = () if tasks else tuple(Modality.from_str(m) for m in body.modalities)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    tier = body.tier or RegistryModel.compute_tier(body.prompt_price, body.completion_price)
    model = RegistryModel(
        service_provider_id=body.service_provider_id,
        model_provider=body.model_provider or body.service_provider_id,
        model_id=body.model_id,
        display_name=body.display_name or body.model_id,
        modalities=modalities,
        tasks=tasks,
        capabilities=tuple(body.capabilities),
        tier=tier,
        prompt_price=body.prompt_price,
        completion_price=body.completion_price,
        image_price=body.image_price,
        audio_price_per_second=body.audio_price_per_second,
        context_length=body.context_length,
        source="manual",
        pricing_mode=body.pricing_mode,
        subscription_id=body.subscription_id,
        price_source=body.price_source or await _provider_pricing_url(core, body.service_provider_id),
        price_source_kind=body.price_source_kind,
        price_checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        description=body.description or "",
    )
    await core.db.upsert_registry_model(model)
    if body.description:
        # The upsert leaves the human-only columns alone (a sync must not erase
        # them), so the text a human typed just now is written separately.
        stored = await core.db.update_registry_model(
            body.service_provider_id, body.model_id, {"description": body.description}
        )
        if stored is not None:
            return model_to_dict(stored)
    return model_to_dict(model)


@api_router.delete("/registry/models")
async def api_delete_model(service_provider_id: str, model_id: str) -> dict[str, Any]:
    core = get_core()
    await core.db.delete_registry_model(service_provider_id, model_id)
    return {"deleted": True, "service_provider_id": service_provider_id, "model_id": model_id}


# --- blob serving -------------------------------------------------------
# Only these render in the browser. Everything else downloads: a caller can put
# an `.html` payload into a response, and serving it inline would run it on the
# same origin as the console, behind the operator's Basic-auth session.
INLINE_MIME_PREFIXES = ("image/", "audio/")


@blob_router.get("/public/blobs/{ref}")
async def serve_public_blob(
    ref: str, request: Request, exp: str | None = None, sig: str | None = None
) -> Response:
    """One blob, by a signed link that expires.

    The only route outside the console's auth gate, and it exists for exactly one
    reason: a provider that takes file inputs by URL (Replicate) has to be able to
    fetch ours, and the inline form is capped at 256 KB. Not an open blob store —
    the signature names one ref and one deadline, and without a signing key
    configured nothing is served at all.
    """
    core = get_core()
    problem = public_blobs.verify(core.settings, ref, exp, sig)
    if problem is not None:
        raise NotFound(problem)
    response = await serve_blob(ref, request)
    # `serve_blob` marks a blob immutable and publicly cacheable, which is right
    # behind the console's password and wrong here: the link expires in minutes
    # and nothing in between should keep a copy of it. Overridden rather than
    # left to nginx's `add_header`, which appends and would leave two
    # contradictory Cache-Control headers for an intermediary to choose between.
    response.headers["Cache-Control"] = "private, no-store"
    return response


@blob_router.get("/blobs/{ref}")
async def serve_blob(ref: str, request: Request) -> Response:
    core = get_core()
    if not core.blobs.exists(ref):
        raise NotFound("Blob not found")
    try:
        data = core.blobs.load(ref)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    mime = core.blobs.mime_of(ref)
    inline = mime.startswith(INLINE_MIME_PREFIXES)
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": (
            f'{"inline" if inline else "attachment"}; filename="{ref}"'
        ),
        # Blob content is immutable — the ref is generated per payload — so the
        # browser may keep it forever. Range support lets the audio player seek
        # instead of refetching the whole file.
        "Cache-Control": "public, max-age=31536000, immutable",
        "Accept-Ranges": "bytes",
    }
    start, end = _parse_range(request.headers.get("range"), len(data))
    if start is not None:
        chunk = data[start : end + 1]
        headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        return Response(content=chunk, status_code=206, media_type=mime, headers=headers)
    return Response(content=data, media_type=mime, headers=headers)


@blob_router.get("/blobs/{ref}/thumb")
async def serve_thumbnail(ref: str) -> Response:
    """A small copy of a stored image, for the gallery and the entry window.

    Same cache rule as the original: the ref names one immutable payload, so
    the browser may keep the preview for as long as it likes. Falls back to the
    original bytes when no preview can be made — see `web/thumbs.py`.
    """
    core = get_core()
    if not core.blobs.exists(ref):
        raise NotFound("Blob not found")
    mime = core.blobs.mime_of(ref)
    if not mime.startswith("image/"):
        raise NotFound("Not an image")
    thumb = thumbs.cached(ref)
    if thumb is None:
        try:
            data = core.blobs.load(ref)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        # Decoding a two-megabyte PNG is tens of milliseconds of CPU; the
        # event loop has runs in flight and should not wait for it.
        thumb = await asyncio.to_thread(thumbs.make, data) or (data, mime)
        thumbs.remember(ref, thumb)
    body, thumb_mime = thumb
    return Response(
        content=body,
        media_type=thumb_mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{ref}"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


def _parse_range(header: str | None, size: int) -> tuple[int | None, int]:
    """Single `bytes=a-b` range, clamped. Anything else means "send it whole"."""
    if not header or not header.startswith("bytes=") or size == 0:
        return None, 0
    spec = header[len("bytes=") :].split(",")[0].strip()
    first, _, last = spec.partition("-")
    try:
        if not first:  # suffix form: last N bytes
            length = int(last)
            if length <= 0:
                return None, 0
            return max(0, size - length), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None, 0
    if start >= size or start > end:
        return None, 0
    return start, min(end, size - 1)


def decode_b64(value: str) -> bytes:
    return base64.b64decode(value)
