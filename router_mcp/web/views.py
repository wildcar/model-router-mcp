"""View models for the console: one shape per thing the console shows.

The `Run` envelope is the important one. The console inspects a run in three
places — right after sending it, when opening a row in the log, and inside the
multi-model table — and all three are the same component. That only works if the
run looks the same whichever way it arrives, so there are exactly two
constructors here (`run_from_dispatch`, `run_from_log`) and a test asserts they
produce the same keys. Diverging shapes are cheap to create and expensive to
merge later: it becomes obvious on the third screen, when rewriting is dear.

See `docs/UI_CONCEPT_v2.md` §17.2 (envelope) and §17.3 (error contract).
"""

from __future__ import annotations

import json
from typing import Any

from router_mcp.domain.errors import RouterError
from router_mcp.domain.messages import RouterRequest
from router_mcp.routing.router import DispatchResult

# Bumped whenever the envelope changes shape. The browser sends it back on every
# request so a page loaded before a restart cannot keep talking to a newer API
# while silently mis-reading the answers.
SCHEMA_VERSION = 1


def _usage(
    *,
    prompt: int | None = None,
    completion: int | None = None,
    total: int | None = None,
    cache_read: int | None = None,
    cache_write: int | None = None,
    reasoning: int | None = None,
    audio_seconds: float | None = None,
    images: int | None = None,
    characters: int | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "completion": completion,
        "total": total,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "reasoning": reasoning,
        "audio_seconds": audio_seconds,
        "images": images,
        "characters": characters,
        "raw": raw or {},
    }


def _cost(value: float | None, source: str | None, breakdown: list[dict[str, Any]] | None = None):
    """Money always travels with its provenance — see §7.1 of the concept.

    A number without `source` is exactly the kind of confidently wrong figure the
    console exists to prevent, so the key is never omitted.
    """
    return {"value": value, "source": source, "breakdown": breakdown or []}


def _run(
    *,
    run_id: str | None,
    log_id: int | None,
    status: str,
    modality: str | None,
    channel: str | None,
    tool: str | None,
    client_id: str | None,
    model: dict[str, Any],
    requested_model: str | None,
    fallback: dict[str, Any] | None,
    usage: dict[str, Any],
    cost: dict[str, Any],
    latency_ms: int | None,
    attempts: int | None,
    provider_status: int | None,
    provider_request_id: str | None,
    finish_reason: str | None,
    created_at: str | None,
    finished_at: str | None,
    text: str | None,
    steps: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    ignored_params: list[str],
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    """The single ordered key set. Both constructors go through here."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "log_id": log_id,
        "status": status,
        "modality": modality,
        "channel": channel,
        "tool": tool,
        "client_id": client_id,
        "model": model,
        "requested_model": requested_model,
        "fallback": fallback,
        "usage": usage,
        "cost": cost,
        "latency_ms": latency_ms,
        "attempts": attempts,
        "provider_status": provider_status,
        "provider_request_id": provider_request_id,
        "finish_reason": finish_reason,
        "created_at": created_at,
        "finished_at": finished_at,
        "text": text,
        "steps": steps,
        "attachments": attachments,
        "ignored_params": ignored_params,
        "error": error,
    }


def _model(provider: str | None, model_id: str | None, display_name: str | None = None):
    # A model is addressed by the pair, never by the id alone: two providers can
    # serve the same id at different prices, and today the lookup takes whichever
    # one sorts first.
    return {
        "provider": provider,
        "model_id": model_id,
        "display_name": display_name or model_id,
    }


def _fallback(requested: str | None, reason: str | None) -> dict[str, Any] | None:
    """Tell a substitution planned before the call from one that followed a
    failure: the first is a dead model id, the second may be a rate limit that
    quietly moved the traffic (and the bill) elsewhere."""
    if not requested and not reason:
        return None
    kind = "after_failure" if reason and " failed: " in reason else "planned"
    return {"kind": kind, "trigger": requested, "reason": reason}


def _attachments_from_response(result: DispatchResult) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for att in result.response.attachments:
        out.append(
            {
                "kind": att.kind,
                "mime": att.mime,
                "url": att.url,
                "ref": att.ref,
                "download_url": f"/blobs/{att.ref}" if att.ref else att.url,
            }
        )
    return out


def run_from_dispatch(
    result: DispatchResult, request: RouterRequest | None = None
) -> dict[str, Any]:
    """Envelope for a run that just executed in this process."""
    response = result.response
    usage = response.usage
    return _run(
        run_id=None,
        log_id=result.log_id,
        status="ok",
        modality=response.modality.value,
        channel=request.channel if request else None,
        tool=request.tool if request else None,
        client_id=request.client_id if request else None,
        model=_model(response.provider, response.model_id),
        requested_model=result.requested_model_id,
        fallback=_fallback(result.requested_model_id, result.fallback_reason),
        usage=_usage(
            prompt=usage.prompt_tokens,
            completion=usage.completion_tokens,
            total=usage.total_tokens,
            cache_read=usage.cache_read_tokens,
            cache_write=usage.cache_write_tokens,
            reasoning=usage.reasoning_tokens,
            audio_seconds=usage.audio_seconds,
            images=usage.images,
            characters=usage.characters,
            raw=usage.raw,
        ),
        cost=_cost(result.cost.cost_usd, result.cost.cost_source,
                   result.cost.breakdown),
        latency_ms=result.latency_ms,
        attempts=result.attempts,
        provider_status=result.provider_status,
        provider_request_id=result.provider_request_id,
        finish_reason=result.finish_reason,
        created_at=None,
        finished_at=result.finished_at,
        text=response.text,
        steps=result.steps,
        attachments=_attachments_from_response(result),
        ignored_params=result.ignored_params,
        error=None,
    )


def run_from_log(row: dict[str, Any]) -> dict[str, Any]:
    """Envelope for a run read back from `request_logs`.

    Rows from the list projection lack the detail columns; the missing keys come
    back as None rather than absent, so the inspector renders "—" instead of
    breaking on a page that was merely paged in.
    """
    ref = row.get("response_ref")
    attachments = (
        [
            {
                "kind": "file",
                "mime": None,
                "url": None,
                "ref": ref,
                "download_url": f"/blobs/{ref}",
            }
        ]
        if ref
        else []
    )
    return _run(
        run_id=None,
        log_id=row.get("id"),
        status=row.get("status") or "ok",
        modality=row.get("modality"),
        channel=row.get("channel"),
        tool=row.get("tool"),
        client_id=row.get("client_id"),
        model=_model(row.get("service_provider_id"), row.get("model_id")),
        requested_model=row.get("requested_model_id"),
        fallback=_fallback(row.get("requested_model_id"), row.get("fallback_reason")),
        usage=_usage(
            prompt=row.get("prompt_tokens"),
            completion=row.get("completion_tokens"),
            total=row.get("total_tokens"),
            cache_read=row.get("cache_read_tokens"),
            cache_write=row.get("cache_write_tokens"),
            reasoning=row.get("reasoning_tokens"),
            audio_seconds=row.get("audio_seconds"),
            images=row.get("images"),
            characters=row.get("characters"),
            raw=_json_or_empty(row.get("usage_raw")),
        ),
        cost=_cost(row.get("cost_usd"), row.get("cost_source")),
        latency_ms=row.get("latency_ms"),
        attempts=row.get("attempts"),
        provider_status=row.get("provider_status"),
        provider_request_id=row.get("provider_request_id"),
        finish_reason=row.get("finish_reason"),
        created_at=row.get("created_at"),
        finished_at=row.get("finished_at"),
        text=row.get("response_text"),
        steps=_json_or_empty(row.get("plan_json"), default=[]),
        attachments=attachments,
        ignored_params=[],
        error=(
            {
                "code": None,
                "message": row.get("error_message"),
                "log_id": row.get("id"),
            }
            if row.get("status") == "error"
            else None
        ),
    )


def _json_or_empty(value: Any, default: Any = None) -> Any:
    """Stored JSON columns are text; a malformed one must not break the page."""
    if default is None:
        default = {}
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def error_envelope(exc: RouterError) -> dict[str, Any]:
    """The one error shape `/api` and the MCP tools both answer with (§17.3).

    `log_id` is the load-bearing field: every error screen offers "open in the
    log", and the row is written before the exception leaves the router.
    """
    return {
        "error": {
            "type": type(exc).__name__,
            "code": exc.code,
            "message": str(exc),
            "provider": exc.provider,
            "model_id": exc.model_id,
            "log_id": exc.log_id,
            "retriable": exc.retriable,
            "provider_status": exc.provider_status,
            "retry_after_s": exc.retry_after_s,
            "hint": exc.hint,
        }
    }


def page(
    items: list[Any],
    *,
    total: int | None = None,
    next_cursor: Any = None,
    facets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """List envelope. A bare array has nowhere to carry the total, the cursor or
    the facet counts, and every list screen in the concept needs all three."""
    return {
        "items": items,
        "total": total,
        "next_cursor": next_cursor,
        "facets": facets or {},
    }
