"""OpenRouter adapter (chat + image-in-chat).

Ports the response-parsing helpers from ort_bot/services/openrouter.py
(text extraction, image-output collection) and adds token-usage capture, which
the original `chat_completion` discards. The HTTP retry/error handling comes
from RetryClientMixin.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from router_mcp.adapters.base import ProviderAdapter, RetryClientMixin
from router_mcp.domain.errors import ProviderError, ProviderUnavailable
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

IMAGE_HTTP_RE = re.compile(r"^https?://.+\.(?:png|jpe?g|webp|gif)(?:\?.*)?$", re.IGNORECASE)


class OpenRouterAdapter(RetryClientMixin, ProviderAdapter):
    name = "openrouter"
    supported_modalities = {Modality.CHAT, Modality.IMAGE}

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
                "HTTP-Referer": "https://localhost",
                "X-Title": "model-router-mcp",
            },
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        messages = self._build_messages(request)
        payload: dict[str, Any] = {"model": model.model_id, "messages": messages}
        for key in ("temperature", "max_tokens", "top_p", "reasoning"):
            if key in request.params:
                payload[key] = request.params[key]

        response = await self._request_with_retry("POST", "/chat/completions", json=payload)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("OpenRouter returned invalid JSON") from exc

        top_error = self._extract_top_level_error(data)
        if top_error is not None:
            message, code = top_error
            if code in (429, 500, 502, 503, 504):
                raise ProviderUnavailable(f"OpenRouter temporarily unavailable: {message}")
            raise ProviderError(message)

        choices = data.get("choices", []) if isinstance(data, dict) else []
        choice0 = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice0.get("message", {}) if isinstance(choice0, dict) else {}
        content = message.get("content") if isinstance(message, dict) else None

        text = self._extract_text(content)
        image_urls = self._collect_image_outputs(choice0) + self._collect_image_outputs(content)
        image_urls = list(dict.fromkeys(u for u in image_urls if u))

        if not text and not image_urls:
            logger.warning(
                "OpenRouter parse miss model=%s sample=%s",
                model.model_id,
                json.dumps(choice0 or data, ensure_ascii=False)[:600],
            )
            raise ProviderError("Model returned an empty response")

        attachments = [Attachment(kind="image", mime="image/png", url=u) for u in image_urls]

        usage = self._parse_usage(data.get("usage") if isinstance(data, dict) else None)
        if image_urls:
            usage.images = len(image_urls)

        return RouterResponse(
            modality=request.modality,
            provider=self.name,
            model_id=model.model_id,
            usage=usage,
            text=text or None,
            attachments=attachments,
            raw_meta={"finish_reason": choice0.get("finish_reason")},
        )

    # --- helpers --------------------------------------------------------
    @staticmethod
    def _build_messages(request: RouterRequest) -> list[dict[str, Any]]:
        if request.messages:
            return request.messages
        if request.text is not None:
            return [{"role": "user", "content": request.text}]
        raise ProviderError("Request has neither `text` nor `messages`")

    @staticmethod
    def _parse_usage(raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        return Usage(
            prompt_tokens=raw.get("prompt_tokens"),
            completion_tokens=raw.get("completion_tokens"),
            total_tokens=raw.get("total_tokens"),
            raw=raw,
        )

    @staticmethod
    def _extract_top_level_error(payload: Any) -> tuple[str, int | str | None] | None:
        if not isinstance(payload, dict):
            return None
        raw_error = payload.get("error")
        if not isinstance(raw_error, dict):
            return None
        message = raw_error.get("message")
        code = raw_error.get("code")
        text = message.strip() if isinstance(message, str) and message.strip() else "OpenRouter error"
        return text, code

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    for key in ("text", "output_text", "content"):
                        val = item.get(key)
                        if isinstance(val, str) and val.strip():
                            parts.append(val.strip())
                            break
            return "\n".join(parts).strip()
        if isinstance(content, dict):
            for key in ("text", "content", "output_text"):
                val = content.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""

    @classmethod
    def _maybe_image_url(cls, value: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        if value.startswith("data:image/"):
            return value
        if IMAGE_HTTP_RE.match(value):
            return value
        return None

    @classmethod
    def _collect_image_outputs(cls, node: Any) -> list[str]:
        found: list[str] = []

        def walk(obj: Any) -> None:
            if isinstance(obj, list):
                for item in obj:
                    walk(item)
                return
            if not isinstance(obj, dict):
                return
            for key in ("url", "image", "image_url"):
                value = obj.get(key)
                if isinstance(value, str):
                    maybe = cls._maybe_image_url(value)
                    if maybe:
                        found.append(maybe)
                elif isinstance(value, dict):
                    inner = value.get("url")
                    if isinstance(inner, str):
                        maybe = cls._maybe_image_url(inner)
                        if maybe:
                            found.append(maybe)
            b64 = obj.get("b64_json")
            if isinstance(b64, str) and b64.strip():
                found.append(f"data:image/png;base64,{b64.strip()}")
            for key in ("inline_data", "source"):
                blob = obj.get(key)
                if isinstance(blob, dict):
                    mime = blob.get("mime_type") or blob.get("media_type") or ""
                    data = blob.get("data")
                    if isinstance(mime, str) and mime.startswith("image/") and isinstance(data, str) and data.strip():
                        found.append(f"data:{mime};base64,{data.strip()}")
            for value in obj.values():
                walk(value)

        walk(node)
        return list(dict.fromkeys(found))
