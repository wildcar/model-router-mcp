"""JSON API consumed by the web UI.

Mirrors the MCP tools but is multipart-friendly (file uploads in later phases)
and adds registry-admin CRUD. Shares the same AppCore as the MCP tools.
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from router_mcp.domain.errors import RouterError
from router_mcp.domain.messages import Attachment, RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.mcp_server.serialize import (
    attachments_web,
    dispatch_summary,
    model_to_dict,
)
from router_mcp.registry.models import RegistryModel
from router_mcp.runtime import get_core

api_router = APIRouter()
blob_router = APIRouter()


class ChatBody(BaseModel):
    external_user_id: str = "web-tester"
    text: str | None = None
    messages: list[dict[str, Any]] | None = None
    model_id: str | None = None
    tier: str | None = None
    provider: str | None = None
    params: dict[str, Any] | None = None


class ImageBody(BaseModel):
    external_user_id: str = "web-tester"
    prompt: str
    model_id: str | None = None
    provider: str | None = None
    params: dict[str, Any] | None = None


class TtsBody(BaseModel):
    external_user_id: str = "web-tester"
    text: str
    voice: str | None = None
    model_id: str | None = None
    provider: str | None = None


class CliBody(BaseModel):
    external_user_id: str = "web-tester"
    prompt: str
    engine: str = "codex"  # "codex" | "claude"
    cwd: str | None = None
    timeout_sec: int | None = None


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


class ProviderPatch(BaseModel):
    enabled: bool


@api_router.get("/models")
async def api_models(
    modality: str | None = None, tier: str | None = None, provider: str | None = None
) -> list[dict[str, Any]]:
    core = get_core()
    models = await core.registry.get_models(modality=modality, tier=tier, provider=provider)
    return [model_to_dict(m) for m in models]


@api_router.post("/chat")
async def api_chat(body: ChatBody) -> dict[str, Any]:
    core = get_core()
    request = RouterRequest(
        modality=Modality.CHAT,
        external_user_id=body.external_user_id,
        model_id=body.model_id,
        tier=body.tier,
        provider_hint=body.provider,
        text=body.text,
        messages=body.messages,
        params=body.params or {},
    )
    try:
        result = await core.router.dispatch(request)
    except RouterError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    out = dispatch_summary(result)
    out["text"] = result.response.text
    if result.response.attachments:
        out["image_urls"] = [a.url for a in result.response.attachments if a.url]
        out["image_refs"] = [a.ref for a in result.response.attachments if a.ref]
    return out


@api_router.post("/image")
async def api_image(body: ImageBody) -> dict[str, Any]:
    core = get_core()
    request = RouterRequest(
        modality=Modality.IMAGE,
        external_user_id=body.external_user_id,
        model_id=body.model_id,
        provider_hint=body.provider,
        text=body.prompt,
        params=body.params or {},
    )
    try:
        result = await core.router.dispatch(request)
    except RouterError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    out = dispatch_summary(result)
    out.update(attachments_web(result.response.attachments))
    return out


@api_router.post("/transcribe")
async def api_transcribe(
    file: UploadFile = File(...),
    external_user_id: str = Form("web-tester"),
    model_id: str | None = Form(None),
    provider: str | None = Form(None),
) -> dict[str, Any]:
    core = get_core()
    audio = await file.read()
    request = RouterRequest(
        modality=Modality.STT,
        external_user_id=external_user_id,
        model_id=model_id,
        provider_hint=provider,
        attachments=[
            Attachment(kind="audio", mime=file.content_type or "audio/ogg", data=audio)
        ],
    )
    try:
        result = await core.router.dispatch(request)
    except RouterError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    out = dispatch_summary(result)
    out["text"] = result.response.text
    return out


@api_router.post("/tts")
async def api_tts(body: TtsBody) -> dict[str, Any]:
    core = get_core()
    params = {"voice": body.voice} if body.voice else {}
    request = RouterRequest(
        modality=Modality.TTS,
        external_user_id=body.external_user_id,
        model_id=body.model_id,
        provider_hint=body.provider,
        text=body.text,
        params=params,
    )
    try:
        result = await core.router.dispatch(request)
    except RouterError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    out = dispatch_summary(result)
    out.update(attachments_web(result.response.attachments))
    return out


@api_router.post("/cli")
async def api_cli(body: CliBody) -> dict[str, Any]:
    core = get_core()
    engine = body.engine.strip().lower()
    if engine not in ("codex", "claude"):
        raise HTTPException(status_code=400, detail="engine must be 'codex' or 'claude'")
    params: dict[str, Any] = {}
    if body.cwd:
        params["cwd"] = body.cwd
    if body.timeout_sec:
        params["timeout_sec"] = body.timeout_sec
    request = RouterRequest(
        modality=Modality.CLI,
        external_user_id=body.external_user_id,
        provider_hint=f"{engine}-cli",
        text=body.prompt,
        params=params,
    )
    try:
        result = await core.router.dispatch(request)
    except RouterError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    out = dispatch_summary(result)
    out["text"] = result.response.text
    out["session_id"] = result.response.raw_meta.get("session_id")
    return out


@api_router.get("/logs")
async def api_logs(user: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    core = get_core()
    return await core.db.list_request_logs(external_user_id=user, limit=limit)


@api_router.get("/usage")
async def api_usage(user: str | None = None, since: str | None = None) -> list[dict[str, Any]]:
    core = get_core()
    rows = await core.db.get_usage(external_user_id=user, since=since)
    return [
        {
            "external_user_id": r.external_user_id,
            "requests": r.requests,
            "total_tokens": r.total_tokens,
            "total_cost_usd": r.total_cost_usd,
        }
        for r in rows
    ]


# --- registry admin -----------------------------------------------------
@api_router.get("/registry/providers")
async def api_providers() -> list[dict[str, Any]]:
    core = get_core()
    providers = await core.db.get_service_providers()
    return [
        {
            "id": p.id,
            "display_name": p.display_name,
            "adapter": p.adapter,
            "enabled": p.enabled,
            "base_url": p.base_url,
        }
        for p in providers
    ]


@api_router.patch("/registry/providers/{provider_id}")
async def api_patch_provider(provider_id: str, body: ProviderPatch) -> dict[str, Any]:
    core = get_core()
    await core.db.set_provider_enabled(provider_id, body.enabled)
    return {"id": provider_id, "enabled": body.enabled}


@api_router.get("/registry/models")
async def api_registry_models(
    source: str | None = None, modality: str | None = None
) -> list[dict[str, Any]]:
    core = get_core()
    models = await core.db.list_registry_models(source=source, modality=modality, active_only=False)
    return [model_to_dict(m) for m in models]


@api_router.post("/registry/models")
async def api_add_manual_model(body: ManualModelBody) -> dict[str, Any]:
    core = get_core()
    try:
        modalities = tuple(Modality.from_str(m) for m in body.modalities)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    )
    await core.db.upsert_registry_model(model)
    return model_to_dict(model)


@api_router.delete("/registry/models")
async def api_delete_model(service_provider_id: str, model_id: str) -> dict[str, Any]:
    core = get_core()
    await core.db.delete_registry_model(service_provider_id, model_id)
    return {"deleted": True, "service_provider_id": service_provider_id, "model_id": model_id}


# --- blob serving -------------------------------------------------------
@blob_router.get("/blobs/{ref}")
async def serve_blob(ref: str) -> Response:
    core = get_core()
    if not core.blobs.exists(ref):
        raise HTTPException(status_code=404, detail="Blob not found")
    try:
        data = core.blobs.load(ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=data, media_type=core.blobs.mime_of(ref))


def decode_b64(value: str) -> bytes:
    return base64.b64decode(value)
