"""DeepSeek adapter — chat (OpenAI-compatible /chat/completions).

DeepSeek's API mirrors the OpenAI chat schema, so this is a thin client over
RetryClientMixin: build messages, POST, read choices[0].message.content and the
usage block.
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


class DeepSeekAdapter(RetryClientMixin, ProviderAdapter):
    name = "deepseek"
    supported_modalities = {Modality.CHAT}

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        timeout_seconds: float = 60.0,
        retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key or ''}",
                "Content-Type": "application/json",
            },
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        messages = self._build_messages(request)
        payload: dict[str, Any] = {"model": model.model_id, "messages": messages}
        for key in ("temperature", "max_tokens", "top_p"):
            if key in request.params:
                payload[key] = request.params[key]

        response = await self._request_with_retry("POST", "/chat/completions", json=payload)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("DeepSeek returned invalid JSON") from exc

        text, usage = self._parse(data)
        return RouterResponse(
            modality=Modality.CHAT,
            provider=self.name,
            model_id=model.model_id,
            usage=usage,
            text=text,
            raw_meta={"finish_reason": self._finish_reason(data)},
        )

    @staticmethod
    def _build_messages(request: RouterRequest) -> list[dict[str, Any]]:
        if request.messages:
            return request.messages
        if request.text is not None:
            return [{"role": "user", "content": request.text}]
        raise ProviderError("Request has neither `text` nor `messages`")

    @staticmethod
    def _finish_reason(data: Any) -> Any:
        choices = data.get("choices", []) if isinstance(data, dict) else []
        return choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None

    @classmethod
    def _parse(cls, data: Any) -> tuple[str, Usage]:
        if not isinstance(data, dict):
            raise ProviderError("DeepSeek returned an unexpected payload")
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ProviderError("DeepSeek returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            raise ProviderError("DeepSeek returned an empty response")
        raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage = Usage(
            prompt_tokens=raw_usage.get("prompt_tokens"),
            completion_tokens=raw_usage.get("completion_tokens"),
            total_tokens=raw_usage.get("total_tokens"),
            raw=raw_usage,
        )
        return text, usage
