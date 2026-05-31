"""Shared serialization between MCP tools and the web JSON API."""

from __future__ import annotations

from typing import Any

from router_mcp.registry.models import RegistryModel
from router_mcp.routing.router import DispatchResult


def model_to_dict(m: RegistryModel) -> dict[str, Any]:
    return {
        "service_provider": m.service_provider_id,
        "model_provider": m.model_provider,
        "model_id": m.model_id,
        "display_name": m.display_name,
        "modalities": [mod.value for mod in m.modalities],
        "capabilities": list(m.capabilities),
        "tier": m.tier,
        "prompt_price": m.prompt_price,
        "completion_price": m.completion_price,
        "image_price": m.image_price,
        "audio_price_per_second": m.audio_price_per_second,
        "context_length": m.context_length,
        "source": m.source,
        "pricing_mode": m.pricing_mode,
    }


def usage_to_dict(result: DispatchResult) -> dict[str, Any]:
    u = result.response.usage
    return {
        "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "total_tokens": u.total_tokens,
        "audio_seconds": u.audio_seconds,
        "images": u.images,
    }


def dispatch_summary(result: DispatchResult) -> dict[str, Any]:
    """The common envelope returned by chat/image/stt/tts/cli tools."""
    return {
        "provider": result.response.provider,
        "model_id": result.response.model_id,
        "modality": result.response.modality.value,
        "usage": usage_to_dict(result),
        "cost_usd": result.cost.cost_usd,
        "cost_source": result.cost.cost_source,
        "pricing_mode": result.cost.pricing_mode,
        "log_id": result.log_id,
    }
