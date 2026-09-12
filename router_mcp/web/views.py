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
import mimetypes
from typing import Any

from router_mcp.domain.errors import RouterError
from router_mcp.domain.messages import RouterRequest
from router_mcp.routing.router import DispatchResult

# Bumped whenever the envelope changes shape. The browser sends it back on every
# request so a page loaded before a restart cannot keep talking to a newer API
# while silently mis-reading the answers.
SCHEMA_VERSION = 3  # 2: the envelope carries `request`; 3: `app` (who called)


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


def _cost(
    value: float | None,
    source: str | None,
    breakdown: list[dict[str, Any]] | None = None,
    provider_value: float | None = None,
    price_factor: float = 1.0,
    price_factor_label: str = "",
):
    """Money always travels with its provenance — see §7.1 of the concept.

    A number without `source` is exactly the kind of confidently wrong figure the
    console exists to prevent, so the key is never omitted. `provider_value` is
    the sum the provider itself reported, when it did: the second opinion the
    reconciliation compares ours against. `price_factor` says the rates in the
    breakdown are not the ones in the price table and why — a peak-hour run whose
    arithmetic silently uses doubled rates reads as a mistake.
    """
    return {
        "value": value,
        "source": source,
        "breakdown": breakdown or [],
        "provider_value": provider_value,
        "price_factor": price_factor,
        "price_factor_label": price_factor_label,
    }


def _run(
    *,
    run_id: str | None,
    log_id: int | None,
    status: str,
    task: str | None,
    modality: str | None,
    channel: str | None,
    tool: str | None,
    client_id: str | None,
    app: str | None,
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
    request: dict[str, Any],
    request_ref: str | None,
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
        # What was asked for, and the group it was billed in. Two facts, both on
        # screen: «текст в картинку» and «картинка в картинку» are one group.
        "task": task,
        "modality": modality,
        "channel": channel,
        "tool": tool,
        "client_id": client_id,
        # Which application asked: the site it runs on, else its name. Empty
        # when the caller did not say — the request then went out as the router
        # itself, and inventing that here would hide who really called.
        "app": app,
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
        # The question, next to the answer. The log kept the prompt text and
        # nothing about how it was asked — no params, no policy, no message list
        # — so «почему ответ такой» had to be guessed from the response alone.
        "request": request,
        # What the request itself carried, when it carried a file. Repeating a
        # transcription from the log needs the audio, and asking the operator to
        # find the file again is asking them to prove it was the same one.
        "request_ref": request_ref,
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


# A stored payload is known to the row by its blob ref alone; the ref carries
# the extension the store chose from the mime at save time (`store.save`), so
# the type can be read back from the name. Without this a generated image read
# from the log was a «file» with no mime, and the inspector offered a download
# link where the «Прогон» screen had shown the picture.
def _media_of(
    ref: str | None, *, kind: str | None = None, mime: str | None = None
) -> tuple[str, str | None]:
    """(kind, mime) for a payload, filling what the row did not keep."""
    if not mime and ref:
        mime = mimetypes.guess_type(ref)[0]
    if not kind or kind == "file":
        if mime and mime.startswith("image/"):
            kind = "image"
        elif mime and mime.startswith("audio/"):
            kind = "audio"
        else:
            kind = kind or "file"
    return kind, mime


def is_image(att: dict[str, Any]) -> bool:
    kind, mime = _media_of(att.get("ref"), kind=att.get("kind"), mime=att.get("mime"))
    return kind == "image" or bool(mime and mime.startswith("image/"))


def _request(
    snapshot: dict[str, Any] | None,
    *,
    text: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """The request side of the envelope, from the stored snapshot.

    Entries written before the snapshot column existed have only `request_text`;
    they get it as the text and `None` everywhere else, so an old row shows what
    it knows instead of an empty tab. The key set never changes — the inspector
    renders "—" for a missing field and must not branch on the row's age.
    """
    snap = snapshot or {}
    attachments = []
    for att in snap.get("attachments") or []:
        att_ref = att.get("ref")
        kind, mime = _media_of(att_ref, kind=att.get("kind"), mime=att.get("mime"))
        attachments.append(
            {
                "kind": kind,
                "role": att.get("role"),
                "mime": mime,
                "bytes": att.get("bytes"),
                "url": att.get("url"),
                "ref": att_ref,
                "download_url": f"/blobs/{att_ref}" if att_ref else att.get("url"),
            }
        )
    # The uploaded payload is on the row as well; when the snapshot lists no
    # attachment (an old entry) the ref alone still gives the operator the file.
    if not attachments and ref:
        kind, mime = _media_of(ref)
        attachments.append(
            {
                "kind": kind,
                "role": None,
                "mime": mime,
                "bytes": None,
                "url": None,
                "ref": ref,
                "download_url": f"/blobs/{ref}",
            }
        )
    return {
        "text": snap.get("text", text) if snapshot else text,
        "text_truncated": bool(snap.get("text_truncated")),
        "messages": snap.get("messages"),
        "dropped_messages": snap.get("dropped_messages") or 0,
        "params": snap.get("params") or {},
        # What the caller asked for; the pair that ran is on `model`.
        "model_id": snap.get("model_id"),
        "tier": snap.get("tier"),
        "provider_hint": snap.get("provider_hint"),
        "fallback": snap.get("fallback"),
        "attachments": attachments,
        "client_request_id": snap.get("client_request_id"),
        "batch_id": snap.get("batch_id"),
        # As the caller sent them; the effective identity is on the run itself.
        "app_url": snap.get("app_url"),
        "app_name": snap.get("app_name"),
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
        task=response.task.value,
        modality=response.modality.value,
        channel=request.channel if request else None,
        tool=request.tool if request else None,
        client_id=request.client_id if request else None,
        app=request.app if request else None,
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
                   result.cost.breakdown,
                   provider_value=result.response.raw_meta.get("provider_cost_usd"),
                   price_factor=result.cost.price_factor,
                   price_factor_label=result.cost.price_factor_label),
        latency_ms=result.latency_ms,
        attempts=result.attempts,
        provider_status=result.provider_status,
        provider_request_id=result.provider_request_id,
        finish_reason=result.finish_reason,
        created_at=None,
        finished_at=result.finished_at,
        text=response.text,
        request=_request(
            result.request_snapshot,
            text=request.text if request else None,
            ref=result.request_ref,
        ),
        request_ref=result.request_ref,
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
    attachments = []
    if ref:
        kind, mime = _media_of(ref)
        attachments.append(
            {
                "kind": kind,
                "mime": mime,
                "url": None,
                "ref": ref,
                "download_url": f"/blobs/{ref}",
            }
        )
    return _run(
        run_id=None,
        log_id=row.get("id"),
        status=row.get("status") or "ok",
        task=row.get("task"),
        modality=row.get("modality"),
        channel=row.get("channel"),
        tool=row.get("tool"),
        client_id=row.get("client_id"),
        app=row.get("app"),
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
        cost=_cost(row.get("cost_usd"), row.get("cost_source"),
                   provider_value=row.get("provider_cost_usd")),
        latency_ms=row.get("latency_ms"),
        attempts=row.get("attempts"),
        provider_status=row.get("provider_status"),
        provider_request_id=row.get("provider_request_id"),
        finish_reason=row.get("finish_reason"),
        created_at=row.get("created_at"),
        finished_at=row.get("finished_at"),
        text=row.get("response_text"),
        request=_request(
            _json_or_empty(row.get("request_json")) or None,
            text=row.get("request_text"),
            ref=row.get("request_ref"),
        ),
        request_ref=row.get("request_ref"),
        steps=_json_or_empty(row.get("plan_json"), default=[]),
        attachments=attachments,
        ignored_params=[],
        error=(
            {
                "code": row.get("error_code"),
                "message": row.get("error_message"),
                "log_id": row.get("id"),
            }
            if row.get("status") == "error"
            else None
        ),
    )


# The gallery caption is cut here, not in the browser: the column holds up to
# 4000 characters and forty of those per page is a page of prompts, not pictures.
_CAPTION_CHARS = 200


def asked_words(row: dict[str, Any]) -> str:
    """The words the caller typed, for a caption.

    The snapshot's `text` first; failing that the last user message of the
    history; failing that `request_text`, which for a chat call is the raw
    message list — its last user message is pulled out the same way. The
    console's table column does this in the browser; the gallery needs it on
    the server because it never fetches the row.
    """
    snap = _json_or_empty(row.get("request_json"))
    text = snap.get("text") if snap else None
    if not text:
        text = _last_user_words(snap.get("messages") if snap else None)
    raw = row.get("request_text") or ""
    if not text and raw.lstrip().startswith(("[", "{")):
        text = _last_user_words(_json_or_empty(raw, default=[]))
    if not text:
        text = raw
    text = " ".join(str(text).split())
    return text[:_CAPTION_CHARS]


def _last_user_words(messages: Any) -> str:
    if isinstance(messages, dict):
        messages = [messages]
    if not isinstance(messages, list):
        return ""

    def words(message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content", message.get("text"))
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                part if isinstance(part, str) else str((part or {}).get("text") or "")
                for part in content
            ).strip()
        return ""

    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user" and words(message):
            return words(message)
    for message in reversed(messages):
        if words(message):
            return words(message)
    return ""


def image_card(row: dict[str, Any]) -> dict[str, Any]:
    """One log row as the gallery sees it: who asked what, and every picture
    that travelled with the request or came back in the answer.

    Request pictures come from the snapshot's attachment list (a ref each, or a
    plain http URL when the caller pointed at one); the answer's picture is the
    row's `response_ref`. Only the first response file is on the row — a call
    that drew several keeps one in the log, and the card says as much as the
    row knows.
    """
    images: list[dict[str, Any]] = []
    snap = _json_or_empty(row.get("request_json"))
    seen: set[str] = set()
    for att in snap.get("attachments") or []:
        if not is_image(att):
            continue
        ref = att.get("ref")
        url = att.get("url")
        if ref:
            key, full, thumb = ref, f"/blobs/{ref}", f"/blobs/{ref}/thumb"
        elif url and url.startswith(("http://", "https://")):
            key, full, thumb = url, url, url
        else:
            continue  # a data: URL was cut to 200 characters — nothing to show
        if key in seen:
            continue
        seen.add(key)
        images.append(
            {
                "side": "request",
                "role": att.get("role"),
                "ref": ref,
                "mime": _media_of(ref, mime=att.get("mime"))[1],
                "url": full,
                "thumb_url": thumb,
            }
        )
    request_ref = row.get("request_ref")
    if request_ref and request_ref not in seen and is_image({"ref": request_ref}):
        images.append(
            {
                "side": "request",
                "role": None,
                "ref": request_ref,
                "mime": _media_of(request_ref)[1],
                "url": f"/blobs/{request_ref}",
                "thumb_url": f"/blobs/{request_ref}/thumb",
            }
        )
    response_ref = row.get("response_ref")
    if response_ref and is_image({"ref": response_ref}):
        images.append(
            {
                "side": "response",
                "role": None,
                "ref": response_ref,
                "mime": _media_of(response_ref)[1],
                "url": f"/blobs/{response_ref}",
                "thumb_url": f"/blobs/{response_ref}/thumb",
            }
        )
    return {
        "log_id": row.get("id"),
        "created_at": row.get("created_at"),
        "external_user_id": row.get("external_user_id"),
        "app": row.get("app"),
        "provider": row.get("service_provider_id"),
        "model_id": row.get("model_id"),
        "task": row.get("task"),
        "status": row.get("status"),
        "text": asked_words(row),
        "images": images,
    }


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
