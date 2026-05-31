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

from router_mcp.domain.messages import RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.mcp_server.serialize import dispatch_summary, model_to_dict
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
) -> dict[str, Any]:
    """Route a chat request to a model and return its text response with usage/cost.

    Provide either `text` (a single user message) or `messages` (OpenAI-shaped
    history). `model_id` pins a specific model; otherwise the router picks one by
    `tier`/`provider` hints.
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
    )
    result = await core.router.dispatch(request)
    out = dispatch_summary(result)
    out["text"] = result.response.text
    if result.response.attachments:
        out["image_urls"] = [a.url for a in result.response.attachments if a.url]
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


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
