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

import base64
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from pydantic import BaseModel

from router_mcp.domain.errors import BadRequest, NotFound
from router_mcp.domain.messages import Attachment, FallbackPolicy, RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.mcp_server.serialize import model_to_dict
from router_mcp.registry.models import RegistryModel
from router_mcp.runtime import get_core
from router_mcp.web.views import page, run_from_dispatch, run_from_log

api_router = APIRouter()
blob_router = APIRouter()


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


class ImageBody(BaseModel):
    external_user_id: str = "web-tester"
    prompt: str
    model_id: str | None = None
    provider: str | None = None
    params: dict[str, Any] | None = None
    fallback: dict[str, Any] | str | None = None


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


class CliBody(BaseModel):
    external_user_id: str = "web-tester"
    prompt: str
    engine: str = "codex"  # "codex" | "claude"
    cwd: str | None = None
    timeout_sec: int | None = None
    # Same gap as tts: `model` (claude-cli) had no way in from the web API.
    params: dict[str, Any] | None = None


class ManualModelBody(BaseModel):
    service_provider_id: str
    model_id: str
    display_name: str = ""
    model_provider: str | None = None
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


class ProviderPatch(BaseModel):
    enabled: bool


@api_router.get("/models")
async def api_models(
    modality: str | None = None, tier: str | None = None, provider: str | None = None
) -> dict[str, Any]:
    core = get_core()
    models = await core.registry.get_models(modality=modality, tier=tier, provider=provider)
    items = [model_to_dict(m) for m in models]
    return page(items, total=len(items))


@api_router.post("/chat")
async def api_chat(body: ChatBody, http_request: Request) -> dict[str, Any]:
    channel, client_id = _channel(http_request)
    return await _run(
        RouterRequest(
            modality=Modality.CHAT,
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
        )
    )


@api_router.post("/image")
async def api_image(body: ImageBody, http_request: Request) -> dict[str, Any]:
    channel, client_id = _channel(http_request)
    return await _run(
        RouterRequest(
            modality=Modality.IMAGE,
            external_user_id=body.external_user_id,
            model_id=body.model_id,
            provider_hint=body.provider,
            text=body.prompt,
            params=body.params or {},
            fallback=parse_fallback(body.fallback),
            channel=channel,
            client_id=client_id,
        )
    )


@api_router.post("/transcribe")
async def api_transcribe(
    http_request: Request,
    file: UploadFile = File(...),
    external_user_id: str = Form("web-tester"),
    model_id: str | None = Form(None),
    provider: str | None = Form(None),
) -> dict[str, Any]:
    audio = await file.read()
    channel, client_id = _channel(http_request)
    return await _run(
        RouterRequest(
            modality=Modality.STT,
            external_user_id=external_user_id,
            model_id=model_id,
            provider_hint=provider,
            attachments=[
                Attachment(kind="audio", mime=file.content_type or "audio/ogg", data=audio)
            ],
            channel=channel,
            client_id=client_id,
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
            modality=Modality.TTS,
            external_user_id=body.external_user_id,
            model_id=body.model_id,
            provider_hint=body.provider,
            text=body.text,
            params=params,
            fallback=parse_fallback(body.fallback),
            channel=channel,
            client_id=client_id,
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
            modality=Modality.CLI,
            external_user_id=body.external_user_id,
            provider_hint=f"{engine}-cli",
            text=body.prompt,
            params=params,
            channel=channel,
            client_id=client_id,
        )
    )


@api_router.get("/logs")
async def api_logs(
    user: str | None = None,
    status: str | None = None,
    modality: str | None = None,
    model_id: str | None = None,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    with_fallback: bool = False,
    with_retries: bool = False,
    unverified_cost: bool = False,
    cursor: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """One page of the log, newest first.

    `cursor` is the id to page *before*: the log grows from the head, so an
    offset would show part of page one again on page two.
    """
    core = get_core()
    limit = max(1, min(limit, 200))
    filters = dict(
        external_user_id=user,
        status=status,
        modality=modality,
        model_id=model_id,
        channel=channel,
        since=since,
        until=until,
        with_fallback=with_fallback,
        with_retries=with_retries,
        unverified_cost=unverified_cost,
    )
    rows = await core.db.list_request_logs(before_id=cursor, limit=limit, **filters)
    total = await core.db.count_request_logs(**filters)
    next_cursor = rows[-1]["id"] if len(rows) == limit else None
    return page(rows, total=total, next_cursor=next_cursor)


@api_router.get("/logs/{log_id}")
async def api_log(log_id: int) -> dict[str, Any]:
    """The full row as a Run envelope — the same shape the inspector renders
    right after a run, so one component serves both."""
    row = await get_core().db.get_request_log(log_id)
    if row is None:
        raise NotFound(f"No log entry {log_id}")
    return run_from_log(row)


@api_router.get("/usage")
async def api_usage(user: str | None = None, since: str | None = None) -> dict[str, Any]:
    core = get_core()
    rows = await core.db.get_usage(external_user_id=user, since=since)
    items = [
        {
            "external_user_id": r.external_user_id,
            "requests": r.requests,
            "total_tokens": r.total_tokens,
            "total_cost_usd": r.total_cost_usd,
        }
        for r in rows
    ]
    return page(items, total=len(items))


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


@api_router.get("/registry/models")
async def api_registry_models(
    source: str | None = None, modality: str | None = None
) -> dict[str, Any]:
    core = get_core()
    models = await core.db.list_registry_models(source=source, modality=modality, active_only=False)
    items = [model_to_dict(m) for m in models]
    return page(items, total=len(items))


@api_router.post("/registry/models")
async def api_add_manual_model(body: ManualModelBody) -> dict[str, Any]:
    core = get_core()
    try:
        modalities = tuple(Modality.from_str(m) for m in body.modalities)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    tier = body.tier or RegistryModel.compute_tier(body.prompt_price, body.completion_price)
    model = RegistryModel(
        service_provider_id=body.service_provider_id,
        model_provider=body.model_provider or body.service_provider_id,
        model_id=body.model_id,
        display_name=body.display_name or body.model_id,
        modalities=modalities,
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
    )
    await core.db.upsert_registry_model(model)
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
