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

from router_mcp.adapters.base import (
    ProviderAdapter,
    RetryClientMixin,
    openai_shaped_usage,
    truncated_error,
)
from router_mcp.domain.errors import ProviderError, ProviderUnavailable
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

# DeepSeek's /models lists ids only; prices live on the docs page and have to be
# transcribed by hand, so catalog rows carry the page as their price source and
# never claim to know a number.
PRICING_PAGE = "https://api-docs.deepseek.com/quick_start/pricing"


class DeepSeekAdapter(RetryClientMixin, ProviderAdapter):
    name = "deepseek"
    env_var = "DEEPSEEK_API_KEY"
    supported_modalities = {Modality.CHAT}
    SUPPORTED_PARAMS = {Modality.CHAT: ("temperature", "max_tokens", "top_p")}

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
        for key in self.SUPPORTED_PARAMS[Modality.CHAT]:
            if key in request.params:
                payload[key] = request.params[key]

        response = await self._request_with_retry("POST", "/chat/completions", json=payload)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("DeepSeek returned invalid JSON") from exc

        text, usage = self._parse(data, max_tokens=payload.get("max_tokens"))
        return RouterResponse(
            modality=Modality.CHAT,
            provider=self.name,
            model_id=model.model_id,
            usage=usage,
            text=text,
            raw_meta={"finish_reason": self._finish_reason(data)},
        )

    async def list_catalog(self) -> list[RegistryModel] | None:
        if not self.is_configured():
            return None
        response = await self._request_with_retry("GET", "/models")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("DeepSeek returned invalid JSON") from exc
        raw = payload.get("data", []) if isinstance(payload, dict) else []
        return [m for m in (self._parse_catalog_model(item) for item in raw) if m is not None]

    @classmethod
    def _parse_catalog_model(cls, raw: Any) -> RegistryModel | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("id") or "").strip()
        if not model_id:
            return None
        return RegistryModel(
            service_provider_id="deepseek",
            model_provider=str(raw.get("owned_by") or "deepseek"),
            model_id=model_id,
            display_name=model_id,
            modalities=(Modality.CHAT,),
            source="catalog",
            is_active=True,
            price_source=PRICING_PAGE,
            price_source_kind="unknown",
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
    def _parse(cls, data: Any, max_tokens: Any = None) -> tuple[str, Usage]:
        if not isinstance(data, dict):
            raise ProviderError("DeepSeek returned an unexpected payload")
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ProviderError("DeepSeek returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        text = content.strip() if isinstance(content, str) else ""
        raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if not text:
            # finish_reason=length with empty content means the ceiling was hit
            # during reasoning, not that the model had nothing to say.
            if cls._finish_reason(data) == "length":
                raise truncated_error(
                    "DeepSeek",
                    max_tokens=max_tokens,
                    completion_tokens=raw_usage.get("completion_tokens"),
                )
            raise ProviderError("DeepSeek returned an empty response")
        # DeepSeek counts cache hits inside `prompt_tokens`
        # (hit + miss == prompt_tokens), which is the contract Usage expects, so
        # the total passes through untouched and only the split is added. A hit
        # is billed about a tenth of a miss, so without the split the cost of a
        # cached conversation is overstated several times over.
        details = openai_shaped_usage(raw_usage)
        usage = Usage(
            prompt_tokens=raw_usage.get("prompt_tokens"),
            completion_tokens=raw_usage.get("completion_tokens"),
            total_tokens=raw_usage.get("total_tokens"),
            cache_read_tokens=details["cache_read"],
            reasoning_tokens=details["reasoning"],
            raw=raw_usage,
        )
        return text, usage
