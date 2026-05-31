"""Claude (Anthropic) adapter — chat via the Messages API.

Raw httpx over RetryClientMixin (no SDK coupling): POST /v1/messages with the
x-api-key + anthropic-version headers. The OpenAI-shaped `messages` are split
into Anthropic's separate `system` string + user/assistant turns. The response
content blocks are concatenated to text; usage maps input/output tokens onto the
normalized prompt/completion fields.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from router_mcp.adapters.base import ProviderAdapter, RetryClientMixin
from router_mcp.domain.errors import ProviderError, ProviderUnavailable
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


class ClaudeApiAdapter(RetryClientMixin, ProviderAdapter):
    name = "anthropic-api"
    supported_modalities = {Modality.CHAT}

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

        text, usage = self._parse(data)
        return RouterResponse(
            modality=Modality.CHAT,
            provider=self.name,
            model_id=model.model_id,
            usage=usage,
            text=text,
            raw_meta={"stop_reason": data.get("stop_reason") if isinstance(data, dict) else None},
        )

    @staticmethod
    def _build_messages(request: RouterRequest) -> tuple[str | None, list[dict[str, Any]]]:
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
    def _parse(cls, data: Any) -> tuple[str, Usage]:
        if not isinstance(data, dict):
            raise ProviderError("Anthropic returned an unexpected payload")
        if data.get("type") == "error":
            err = data.get("error") or {}
            raise ProviderError(str(err.get("message") or "Anthropic returned an error"))
        blocks = data.get("content") or []
        parts = [
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        text = "".join(parts).strip()
        if not text:
            raise ProviderError("Anthropic returned an empty response")
        raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt = raw_usage.get("input_tokens")
        completion = raw_usage.get("output_tokens")
        total = (prompt or 0) + (completion or 0) if (prompt or completion) else None
        usage = Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            raw=raw_usage,
        )
        return text, usage
