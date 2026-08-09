"""Shared serialization between MCP tools and the web JSON API."""

from __future__ import annotations

import base64
from typing import Any

from router_mcp.domain.messages import Attachment
from router_mcp.registry.models import RegistryModel
from router_mcp.routing.router import DispatchResult


def model_to_dict(m: RegistryModel) -> dict[str, Any]:
    return {
        "service_provider": m.service_provider_id,
        "model_provider": m.model_provider,
        "model_id": m.model_id,
        "display_name": m.display_name,
        "tasks": [task.value for task in m.tasks],
        "modalities": [mod.value for mod in m.modalities],
        "capabilities": list(m.capabilities),
        "tier": m.tier,
        "prompt_price": m.prompt_price,
        "completion_price": m.completion_price,
        "image_price": m.image_price,
        "audio_price_per_second": m.audio_price_per_second,
        "character_price": m.character_price,
        "compute_price_per_second": m.compute_price_per_second,
        "cache_read_price": m.cache_read_price,
        "cache_write_price": m.cache_write_price,
        "web_search_price": m.web_search_price,
        "context_length": m.context_length,
        "source": m.source,
        "pricing_mode": m.pricing_mode,
        "is_active": m.is_active,
        "last_seen_at": m.last_seen_at,
        "deprecated_at": m.deprecated_at,
        # When a request to this pair last came back with an answer. Null means
        # the id is listed or seeded but never actually served anything here.
        "confirmed_at": m.confirmed_at,
        "replaced_by": m.replaced_by,
        # Price provenance: a caller comparing costs needs to know which numbers
        # are confirmed and which are guesses.
        "price_source": m.price_source,
        "price_source_kind": m.price_source_kind,
        "price_checked_at": m.price_checked_at,
        "price_is_verified": m.price_is_verified,
        "price_is_unknown": m.price_is_unknown,
        "updated_at": m.updated_at,
        # Which of this model's input properties each attachment role binds to.
        # Empty means positional binding, which is every single-image model.
        "input_map": dict(m.input_map),
        # Knobs this model accepts beyond the adapter's declared set.
        "extra_params": list(m.extra_params),
        # Sent back on an edit as an optimistic lock: a second-resolution
        # timestamp cannot tell two edits in the same second apart.
        "revision": m.revision,
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


def _attachment_bytes(att: Attachment, blobs) -> bytes | None:
    if att.data is not None:
        return att.data
    if att.ref and blobs is not None and blobs.exists(att.ref):
        return blobs.load(att.ref)
    return None


def attachments_b64(attachments: list[Attachment], blobs) -> dict[str, Any]:
    """For MCP clients: inline images/audio as base64 (urls kept when remote)."""
    image_urls: list[str] = []
    image_b64: list[str] = []
    audio_b64: list[str] = []
    audio_mime: str | None = None
    for att in attachments:
        if att.kind == "image":
            if att.url:
                image_urls.append(att.url)
                continue
            data = _attachment_bytes(att, blobs)
            if data is not None:
                image_b64.append(base64.b64encode(data).decode("ascii"))
        elif att.kind == "audio":
            data = _attachment_bytes(att, blobs)
            if data is not None:
                audio_b64.append(base64.b64encode(data).decode("ascii"))
                audio_mime = att.mime
    out: dict[str, Any] = {}
    if image_urls:
        out["image_urls"] = image_urls
    if image_b64:
        out["image_b64"] = image_b64
    if audio_b64:
        out["audio_b64"] = audio_b64[0] if len(audio_b64) == 1 else audio_b64
        out["audio_mime"] = audio_mime
    return out


def attachments_web(attachments: list[Attachment]) -> dict[str, Any]:
    """For the browser UI: prefer blob refs (served via /blobs/{ref}); keep
    remote/data urls as-is. Never inlines large base64 into JSON."""
    image_urls: list[str] = []
    image_refs: list[str] = []
    audio_ref: str | None = None
    for att in attachments:
        if att.kind == "image":
            if att.url:
                image_urls.append(att.url)
            elif att.ref:
                image_refs.append(att.ref)
        elif att.kind == "audio" and att.ref:
            audio_ref = att.ref
    out: dict[str, Any] = {}
    if image_urls:
        out["image_urls"] = image_urls
    if image_refs:
        out["image_refs"] = image_refs
    if audio_ref:
        out["audio_ref"] = audio_ref
    return out


def dispatch_summary(result: DispatchResult) -> dict[str, Any]:
    """The common envelope returned by chat/image/stt/tts/cli tools.

    `requested_model_id` / `fallback_reason` are non-null only when a fallback
    policy substituted a model: a caller must never discover a substitution by
    reading its bill.
    """
    return {
        "provider": result.response.provider,
        "model_id": result.response.model_id,
        "requested_model_id": result.requested_model_id,
        "fallback_reason": result.fallback_reason,
        "task": result.response.task.value,
        "modality": result.response.modality.value,
        "usage": usage_to_dict(result),
        "cost_usd": result.cost.cost_usd,
        "cost_source": result.cost.cost_source,
        "pricing_mode": result.cost.pricing_mode,
        "log_id": result.log_id,
    }
