"""Claude (Anthropic) adapter — chat via the Messages API.

Raw httpx over RetryClientMixin (no SDK coupling): POST /v1/messages with the
x-api-key + anthropic-version headers. The OpenAI-shaped `messages` are split
into Anthropic's separate `system` string + user/assistant turns. The response
content blocks are concatenated to text; usage maps input/output tokens onto the
normalized prompt/completion fields.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from router_mcp.adapters.base import (
    ProviderAdapter,
    RetryClientMixin,
    provider_error_for,
    truncated_error,
)
from router_mcp.domain.errors import ContentFiltered, ProviderError, ProviderUnavailable
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)


ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096
# /v1/models returns ids and display names, no prices.
PRICING_PAGE = "https://claude.com/pricing#api"


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class ClaudeApiAdapter(RetryClientMixin, ProviderAdapter):
    name = "anthropic-api"
    env_var = "ANTHROPIC_API_KEY"
    # Anthropic's Messages API takes images in a user turn, so vision is real
    # here. It never produces one, so no image task is declared.
    supported_tasks = {Task.TEXT_TO_TEXT, Task.IMAGE_TO_TEXT}
    # max_tokens is mandatory here (unlike the OpenAI-shaped providers) and has
    # a default, so it is listed even though it is never merely passed through.
    _CHAT_PARAMS = ("temperature", "top_p", "max_tokens")
    SUPPORTED_PARAMS = {Task.TEXT_TO_TEXT: _CHAT_PARAMS, Task.IMAGE_TO_TEXT: _CHAT_PARAMS}

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        timeout_seconds: float = 120.0,
        retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "x-api-key": api_key or "",
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        system, messages = self._build_messages(request)
        payload: dict[str, Any] = {
            "model": model.model_id,
            "max_tokens": int(request.params.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "messages": messages,
        }
        if system:
            payload["system"] = system
        for key in ("temperature", "top_p"):
            if key in request.params:
                payload[key] = request.params[key]

        response = await self._request_with_retry("POST", "/v1/messages", json=payload)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("Anthropic returned invalid JSON") from exc

        text, usage = self._parse(data, max_tokens=payload["max_tokens"])
        return RouterResponse(
            task=request.task,
            provider=self.name,
            model_id=model.model_id,
            usage=usage,
            text=text,
            raw_meta={"stop_reason": data.get("stop_reason") if isinstance(data, dict) else None},
        )

    async def list_catalog(self) -> list[RegistryModel] | None:
        if not self.is_configured():
            return None
        response = await self._request_with_retry("GET", "/v1/models?limit=1000")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("Anthropic returned invalid JSON") from exc
        raw = payload.get("data", []) if isinstance(payload, dict) else []
        models: list[RegistryModel] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            models.append(
                RegistryModel(
                    service_provider_id="anthropic-api",
                    model_provider="anthropic",
                    model_id=model_id,
                    display_name=str(item.get("display_name") or model_id),
                    tasks=(Task.TEXT_TO_TEXT,),
                    source="catalog",
                    is_active=True,
                    price_source=PRICING_PAGE,
                    price_source_kind="unknown",
                )
            )
        return models

    @staticmethod
    def _build_messages(request: RouterRequest) -> tuple[str | None, list[dict[str, Any]]]:
        """`(system, turns)`, with images as Anthropic content blocks.

        Its own shape, not the OpenAI one: `{"type": "image", "source": {...}}`
        with the bytes base64-encoded and the media type named separately.
        Attachments used to be dropped here silently, so a vision request arrived
        as its text alone.
        """
        images = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": att.mime,
                    "data": base64.b64encode(att.data).decode(),
                },
            }
            for att in request.attachments
            if att.kind == "image" and att.data
        ]
        if images and not request.messages:
            parts: list[dict[str, Any]] = list(images)
            if request.text:
                parts.append({"type": "text", "text": request.text})
            return None, [{"role": "user", "content": parts}]
        if request.messages:
            system_parts: list[str] = []
            turns: list[dict[str, Any]] = []
            for m in request.messages:
                role = m.get("role")
                content = m.get("content")
                if role == "system":
                    if isinstance(content, str):
                        system_parts.append(content)
                    continue
                turns.append({"role": role, "content": content})
            return ("\n".join(system_parts) or None), turns
        if request.text is not None:
            return None, [{"role": "user", "content": request.text}]
        raise ProviderError("Request has neither `text` nor `messages`")

    @classmethod
    def _parse(cls, data: Any, max_tokens: Any = None) -> tuple[str, Usage]:
        if not isinstance(data, dict):
            raise ProviderError("Anthropic returned an unexpected payload")
        if data.get("type") == "error":
            err = data.get("error") or {}
            raise provider_error_for(str(err.get("message") or "Anthropic returned an error"))
        blocks = data.get("content") or []
        parts = [
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        text = "".join(parts).strip()
        raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if not text:
            if data.get("stop_reason") == "max_tokens":
                raise truncated_error(
                    "Anthropic",
                    max_tokens=max_tokens,
                    completion_tokens=raw_usage.get("output_tokens"),
                )
            if data.get("stop_reason") == "refusal":
                raise ContentFiltered("Anthropic declined to answer (stop_reason=refusal)")
            raise ProviderError("Anthropic returned an empty response")
        # Anthropic reports cache reads and writes *outside* `input_tokens`,
        # unlike the OpenAI-shaped providers. Usage's contract is that
        # `prompt_tokens` is the whole prompt, so they are added back here —
        # otherwise a cached conversation looks like it sent almost no prompt.
        cache_read = _int(raw_usage.get("cache_read_input_tokens"))
        cache_write = _int(raw_usage.get("cache_creation_input_tokens"))
        input_tokens = _int(raw_usage.get("input_tokens"))
        prompt = (input_tokens or 0) + (cache_read or 0) + (cache_write or 0) or None
        completion = _int(raw_usage.get("output_tokens"))
        total = (prompt or 0) + (completion or 0) if (prompt or completion) else None
        usage = Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            raw=raw_usage,
        )
        return text, usage
