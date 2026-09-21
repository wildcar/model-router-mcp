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
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from router_mcp.domain.errors import BadRequest, RouterError
from router_mcp.domain.messages import Attachment, FallbackPolicy, RouterRequest
from router_mcp.domain.task import Task
from router_mcp.mcp_server.serialize import (
    attachments_b64,
    dispatch_summary,
    model_to_dict,
)
from router_mcp.runtime import get_core

# streamable_http_path="/" so that mounting the sub-app at "/mcp" yields the
# endpoint at "/mcp" (not "/mcp/mcp").
mcp = FastMCP("model-router", stateless_http=True, streamable_http_path="/")


async def dispatch_tool(request: RouterRequest):
    """Dispatch and translate a failure into the shared error contract.

    FastMCP hands the client `str(exception)`, so the fields the web API puts in
    the error envelope — the stable code, the log id, the environment variable
    to set — are appended to the message. The caller here is usually a model
    reading text; without them "provider not configured" is a dead end.
    """
    try:
        return await get_core().router.dispatch(request)
    except RouterError as exc:
        facts = [f"code={exc.code}"]
        if exc.log_id is not None:
            facts.append(f"log_id={exc.log_id}")
        if exc.provider:
            facts.append(f"provider={exc.provider}")
        if exc.provider_status:
            facts.append(f"provider_status={exc.provider_status}")
        if exc.retry_after_s is not None:
            facts.append(f"retry_after_s={exc.retry_after_s}")
        if exc.hint and exc.hint.get("env_var"):
            facts.append(f"env_var={exc.hint['env_var']}")
        raise type(exc)(f"{exc} [{' '.join(facts)}]") from exc


@mcp.tool()
async def list_models(
    task: str | None = None,
    modality: str | None = None,
    tier: str | None = None,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    """List registered models, optionally filtered by task/tier/provider.

    `task` is what a model can be asked to do: `text-to-text`, `image-to-text`,
    `text-to-image`, `image-to-image`, `speech-to-text`, `text-to-speech`,
    `text-to-audio`, `text-to-video`, `image-to-video`, `text-to-decisions`,
    `cli`. `modality` is the
    coarser billing group (`chat`/`image`/`stt`/`tts`/`audio`/`video`/`cli`) and
    is kept for callers written before tasks existed.
    """
    core = get_core()
    models = await core.registry.get_models(
        task=task, modality=modality, tier=tier, provider=provider
    )
    return [model_to_dict(m) for m in models]


@mcp.tool()
async def chat(
    external_user_id: str,
    text: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    images_b64: list[str] | None = None,
    image_mime: str = "image/png",
    model_id: str | None = None,
    tier: str | None = None,
    provider: str | None = None,
    params: dict[str, Any] | None = None,
    fallback: dict[str, Any] | str | None = None,
    app_url: str | None = None,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Route a chat request to a model and return its text response with usage/cost.

    Provide either `text` (a single user message) or `messages` (OpenAI-shaped
    history). `model_id` pins a specific model; otherwise the router picks one by
    `tier`/`provider` hints.

    `app_url` / `app_name` identify the application making the request: the site
    it runs on wins, a plain name is the fallback. The identity is logged, the
    journal filters by it, and providers that take such a parameter receive it
    (OpenRouter's HTTP-Referer / X-Title). Omitted, the request is made as the
    router itself. Note: OpenRouter attributes by the URL's *domain* and cuts
    the path — a page on someone else's site (https://dzen.ru/yourblog) is
    recorded as that domain's app, so pass a domain your app owns.

    Pass `images_b64` to ask about pictures. The task then becomes `image-to-text`
    and only models that declare it are eligible — a model that can *draw* is not
    thereby able to *look*, so the two are routed separately.

    `fallback` covers the case where the pinned model was retired or is down:
    `{"policy": "same"|"cheaper"|"pricier", "max_steps": 1, "scope":
    "provider"|"vendor"|"any"}` (or just the policy name). The reply then carries
    `requested_model_id` and `fallback_reason` alongside the model actually used.
    """
    attachments = [
        Attachment(kind="image", mime=image_mime, data=base64.b64decode(raw))
        for raw in images_b64 or []
    ]
    request = RouterRequest(
        # The task follows from what arrived rather than from the tool name: one
        # tool, two tasks, and the caller does not have to know the vocabulary.
        task=Task.IMAGE_TO_TEXT if attachments else Task.TEXT_TO_TEXT,
        attachments=attachments,
        external_user_id=external_user_id,
        model_id=model_id,
        tier=tier,
        provider_hint=provider,
        text=text,
        messages=messages,
        params=params or {},
        fallback=FallbackPolicy.parse(fallback),
        channel="mcp",
        tool="chat",
        app_url=app_url,
        app_name=app_name,
    )
    result = await dispatch_tool(request)
    out = dispatch_summary(result)
    out["text"] = result.response.text
    if result.response.attachments:
        out["image_urls"] = [a.url for a in result.response.attachments if a.url]
    # Present only when `params.web_search` was honoured: which search actually
    # ran ("server_tool" | "plugin" | "off") — the adapter may have degraded to
    # no search at all, and the caller must hear that from the reply, not guess
    # it from an answer that cites nothing.
    if "web_search_mode" in result.response.raw_meta:
        out["web_search_mode"] = result.response.raw_meta["web_search_mode"]
    return out


@mcp.tool()
async def decide(
    external_user_id: str,
    questions: dict[str, Any],
    state: str | dict[str, Any] | list[Any] | None = None,
    text: str | None = None,
    model_id: str | None = None,
    provider: str | None = None,
    fallback: dict[str, Any] | str | None = None,
    app_url: str | None = None,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Ask a decision model typed questions about a text (`text-to-decisions`).

    Not a chat: the model (TypeSafe Jev via OpenRouter, ~0.25 s, input tokens
    only are billed) returns probabilities, not prose. Give it the `state` to
    judge — a string, or a JSON object/array of related context — and a
    `questions` object, name → question. Three question types:

      {"type": "noul", "instructions": "Is the customer asking for a refund?",
       "criteria": {"true": "...", "false": "..."}}          # criteria optional
      {"type": "choice", "instructions": "Which team owns this?",
       "criteria": {"billing": "Charges and refunds", "tech": "Bugs"}}
      {"type": "score", "instructions": "How urgent?",
       "criteria": ["Can wait", "This week", "Blocking revenue"]}  # ordered

    The reply carries `answers`, name → {type, noul: 0.96} | {type, choice,
    confidence, probabilities} | {type, score, confidence, probabilities,
    legend}, plus the usual usage and cost. `text` is accepted as an alias for
    a string `state`. `fallback` and `app_url`/`app_name` work as in `chat`.
    """
    if state is None or state == "":
        state = text
    if state is None or state == "":
        raise BadRequest("Provide the text to judge in `state` (or `text`)")
    params: dict[str, Any] = {"questions": questions}
    if isinstance(state, str):
        prompt = state
    else:
        # The structure travels to the provider as-is; the log gets it as text.
        params["state"] = state
        prompt = json.dumps(state, ensure_ascii=False)
    request = RouterRequest(
        task=Task.TEXT_TO_DECISIONS,
        external_user_id=external_user_id,
        model_id=model_id,
        provider_hint=provider,
        text=prompt,
        params=params,
        fallback=FallbackPolicy.parse(fallback),
        channel="mcp",
        tool="decide",
        app_url=app_url,
        app_name=app_name,
    )
    result = await dispatch_tool(request)
    out = dispatch_summary(result)
    out["answers"] = result.response.raw_meta.get("answers")
    # `~typesafe/jev-latest` is an alias; this is the dated release that answered.
    out["served_model_id"] = result.response.raw_meta.get("served_model_id")
    return out


@mcp.tool()
async def generate_image(
    external_user_id: str,
    prompt: str,
    model_id: str | None = None,
    provider: str | None = None,
    params: dict[str, Any] | None = None,
    fallback: dict[str, Any] | str | None = None,
    app_url: str | None = None,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Generate an image from a text prompt. Returns image(s) as base64 and/or
    URLs, with usage and cost. `fallback` and `app_url`/`app_name` work as in
    `chat`."""
    core = get_core()
    request = RouterRequest(
        task=Task.TEXT_TO_IMAGE,
        external_user_id=external_user_id,
        model_id=model_id,
        provider_hint=provider,
        text=prompt,
        params=params or {},
        fallback=FallbackPolicy.parse(fallback),
        channel="mcp",
        tool="generate_image",
        app_url=app_url,
        app_name=app_name,
    )
    result = await dispatch_tool(request)
    out = dispatch_summary(result)
    out.update(attachments_b64(result.response.attachments, core.blobs))
    return out


@mcp.tool()
async def edit_image(
    external_user_id: str,
    prompt: str,
    image_b64: str,
    mime: str = "image/png",
    mask_b64: str | None = None,
    model_id: str | None = None,
    provider: str | None = None,
    params: dict[str, Any] | None = None,
    fallback: dict[str, Any] | str | None = None,
    app_url: str | None = None,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Change an existing image according to a text instruction (`image-to-image`).

    A separate tool from `generate_image` because it is a separate capability:
    most models that draw from a description cannot edit a picture they are given,
    and asking one to do so used to route silently to a generator, which returned
    a brand-new image instead of the edit. If nothing configured here supports it,
    the call fails saying so.

    `mask_b64` marks the region to change, where the model accepts one.
    `fallback` and `app_url`/`app_name` work as in `chat`.
    """
    core = get_core()
    attachments = [Attachment(kind="image", mime=mime, data=base64.b64decode(image_b64))]
    if mask_b64:
        attachments.append(
            Attachment(kind="image", mime=mime, data=base64.b64decode(mask_b64))
        )
    request = RouterRequest(
        task=Task.IMAGE_TO_IMAGE,
        external_user_id=external_user_id,
        model_id=model_id,
        provider_hint=provider,
        text=prompt,
        attachments=attachments,
        params=params or {},
        fallback=FallbackPolicy.parse(fallback),
        channel="mcp",
        tool="edit_image",
        app_url=app_url,
        app_name=app_name,
    )
    result = await dispatch_tool(request)
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
    app_url: str | None = None,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Transcribe base64-encoded audio to text (speech-to-text).
    `fallback` and `app_url`/`app_name` work as in `chat`."""
    request = RouterRequest(
        task=Task.SPEECH_TO_TEXT,
        external_user_id=external_user_id,
        model_id=model_id,
        provider_hint=provider,
        attachments=[Attachment(kind="audio", mime=mime, data=base64.b64decode(audio_b64))],
        fallback=FallbackPolicy.parse(fallback),
        channel="mcp",
        tool="transcribe",
        app_url=app_url,
        app_name=app_name,
    )
    result = await dispatch_tool(request)
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
    app_url: str | None = None,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Synthesize speech from text (text-to-speech). Returns base64 audio.
    `fallback` and `app_url`/`app_name` work as in `chat`."""
    core = get_core()
    merged = dict(params or {})
    if voice:
        merged["voice"] = voice
    request = RouterRequest(
        task=Task.TEXT_TO_SPEECH,
        external_user_id=external_user_id,
        model_id=model_id,
        provider_hint=provider,
        text=text,
        params=merged,
        fallback=FallbackPolicy.parse(fallback),
        channel="mcp",
        tool="synthesize_speech",
        app_url=app_url,
        app_name=app_name,
    )
    result = await dispatch_tool(request)
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
    app_url: str | None = None,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Run a prompt through a local CLI engine (`codex` or `claude`).

    `cwd` (optional) must be under a server-allowed root. Returns the engine's
    text output with usage and cost. `app_url`/`app_name` work as in `chat`.
    """
    engine = engine.strip().lower()
    if engine not in ("codex", "claude"):
        raise BadRequest("engine must be 'codex' or 'claude'")
    params: dict[str, Any] = {}
    if cwd:
        params["cwd"] = cwd
    if timeout_sec:
        params["timeout_sec"] = timeout_sec
    request = RouterRequest(
        task=Task.CLI,
        external_user_id=external_user_id,
        provider_hint=f"{engine}-cli",
        text=prompt,
        params=params,
        channel="mcp",
        tool="run_cli",
        app_url=app_url,
        app_name=app_name,
    )
    result = await dispatch_tool(request)
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
