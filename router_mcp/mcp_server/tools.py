"""FastMCP tool definitions.

Each tool is a thin shell: build a RouterRequest, call ModelRouter.dispatch,
shape the result. The external user id is an explicit argument — MCP clients are
themselves the "user" of this server, so the end-user id is application data the
caller must forward.

Binary payloads cross MCP as base64 (audio_b64, image_b64), since tool results
are JSON.
"""

from __future__ import annotations

import base64
from typing import Any

from mcp.server.fastmcp import FastMCP

from router_mcp.domain.messages import Attachment, FallbackPolicy, RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.mcp_server.serialize import (
    attachments_b64,
    dispatch_summary,
    model_to_dict,
)
from router_mcp.runtime import get_core

# streamable_http_path="/" so that mounting the sub-app at "/mcp" yields the
# endpoint at "/mcp" (not "/mcp/mcp").
mcp = FastMCP("model-router", stateless_http=True, streamable_http_path="/")


@mcp.tool()
async def list_models(
    modality: str | None = None,
    tier: str | None = None,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    """List registered models, optionally filtered by modality/tier/provider."""
    core = get_core()
    models = await core.registry.get_models(modality=modality, tier=tier, provider=provider)
    return [model_to_dict(m) for m in models]


@mcp.tool()
async def chat(
    external_user_id: str,
    text: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    model_id: str | None = None,
    tier: str | None = None,
    provider: str | None = None,
    params: dict[str, Any] | None = None,
    fallback: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Route a chat request to a model and return its text response with usage/cost.

    Provide either `text` (a single user message) or `messages` (OpenAI-shaped
    history). `model_id` pins a specific model; otherwise the router picks one by
    `tier`/`provider` hints.

    `fallback` covers the case where the pinned model was retired or is down:
    `{"policy": "same"|"cheaper"|"pricier", "max_steps": 1, "scope":
    "provider"|"vendor"|"any"}` (or just the policy name). The reply then carries
    `requested_model_id` and `fallback_reason` alongside the model actually used.
    """
    core = get_core()
    request = RouterRequest(
        modality=Modality.CHAT,
        external_user_id=external_user_id,
        model_id=model_id,
        tier=tier,
        provider_hint=provider,
        text=text,
        messages=messages,
        params=params or {},
        fallback=FallbackPolicy.parse(fallback),
    )
    result = await core.router.dispatch(request)
    out = dispatch_summary(result)
    out["text"] = result.response.text
    if result.response.attachments:
        out["image_urls"] = [a.url for a in result.response.attachments if a.url]
    return out


@mcp.tool()
async def generate_image(
    external_user_id: str,
    prompt: str,
    model_id: str | None = None,
    provider: str | None = None,
    params: dict[str, Any] | None = None,
    fallback: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Generate an image from a text prompt. Returns image(s) as base64 and/or
    URLs, with usage and cost. `fallback` works as in `chat`."""
    core = get_core()
    request = RouterRequest(
        modality=Modality.IMAGE,
        external_user_id=external_user_id,
        model_id=model_id,
        provider_hint=provider,
        text=prompt,
        params=params or {},
        fallback=FallbackPolicy.parse(fallback),
    )
    result = await core.router.dispatch(request)
    out = dispatch_summary(result)
    out.update(attachments_b64(result.response.attachments, core.blobs))
    return out


@mcp.tool()
async def transcribe(
    external_user_id: str,
    audio_b64: str,
    mime: str = "audio/ogg",
    model_id: str | None = None,
    provider: str | None = None,
    fallback: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Transcribe base64-encoded audio to text (speech-to-text).
    `fallback` works as in `chat`."""
    core = get_core()
    request = RouterRequest(
        modality=Modality.STT,
        external_user_id=external_user_id,
        model_id=model_id,
        provider_hint=provider,
        attachments=[Attachment(kind="audio", mime=mime, data=base64.b64decode(audio_b64))],
        fallback=FallbackPolicy.parse(fallback),
    )
    result = await core.router.dispatch(request)
    out = dispatch_summary(result)
    out["text"] = result.response.text
    return out


@mcp.tool()
async def synthesize_speech(
    external_user_id: str,
    text: str,
    voice: str | None = None,
    model_id: str | None = None,
    provider: str | None = None,
    params: dict[str, Any] | None = None,
    fallback: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Synthesize speech from text (text-to-speech). Returns base64 audio.
    `fallback` works as in `chat`."""
    core = get_core()
    merged = dict(params or {})
    if voice:
        merged["voice"] = voice
    request = RouterRequest(
        modality=Modality.TTS,
        external_user_id=external_user_id,
        model_id=model_id,
        provider_hint=provider,
        text=text,
        params=merged,
        fallback=FallbackPolicy.parse(fallback),
    )
    result = await core.router.dispatch(request)
    out = dispatch_summary(result)
    out.update(attachments_b64(result.response.attachments, core.blobs))
    return out


@mcp.tool()
async def run_cli(
    external_user_id: str,
    prompt: str,
    engine: str = "codex",
    cwd: str | None = None,
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    """Run a prompt through a local CLI engine (`codex` or `claude`).

    `cwd` (optional) must be under a server-allowed root. Returns the engine's
    text output with usage and cost.
    """
    core = get_core()
    engine = engine.strip().lower()
    if engine not in ("codex", "claude"):
        raise ValueError("engine must be 'codex' or 'claude'")
    params: dict[str, Any] = {}
    if cwd:
        params["cwd"] = cwd
    if timeout_sec:
        params["timeout_sec"] = timeout_sec
    request = RouterRequest(
        modality=Modality.CLI,
        external_user_id=external_user_id,
        provider_hint=f"{engine}-cli",
        text=prompt,
        params=params,
    )
    result = await core.router.dispatch(request)
    out = dispatch_summary(result)
    out["text"] = result.response.text
    out["session_id"] = result.response.raw_meta.get("session_id")
    return out


@mcp.tool()
async def get_usage(
    external_user_id: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregated per-user usage: request count, total tokens, total cost (USD)."""
    core = get_core()
    rows = await core.db.get_usage(external_user_id=external_user_id, since=since)
    return [
        {
            "external_user_id": r.external_user_id,
            "requests": r.requests,
            "total_tokens": r.total_tokens,
            "total_cost_usd": r.total_cost_usd,
        }
        for r in rows
    ]
