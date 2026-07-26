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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from router_mcp.adapters.base import (
    ProviderAdapter,
    RetryClientMixin,
    openai_shaped_usage,
    truncated_error,
)
from router_mcp.domain.errors import ProviderError, ProviderUnavailable
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

IMAGE_HTTP_RE = re.compile(r"^https?://.+\.(?:png|jpe?g|webp|gif)(?:\?.*)?$", re.IGNORECASE)

# OpenRouter is the one provider whose catalog carries prices, so rows it
# produces are the only ones marked as API-sourced.
PRICE_SOURCE = "api:openrouter GET /models (pricing)"


class OpenRouterAdapter(RetryClientMixin, ProviderAdapter):
    name = "openrouter"
    env_var = "OPENROUTER_API_KEY"
    # Everything goes through /chat/completions, including the models that answer
    # with an image, so the knobs are the same for all three.
    supported_tasks = {Task.TEXT_TO_TEXT, Task.IMAGE_TO_TEXT, Task.TEXT_TO_IMAGE}
    _CHAT_PARAMS = ("temperature", "max_tokens", "top_p", "reasoning")
    SUPPORTED_PARAMS = {
        Task.TEXT_TO_TEXT: _CHAT_PARAMS,
        Task.IMAGE_TO_TEXT: _CHAT_PARAMS,
        Task.TEXT_TO_IMAGE: _CHAT_PARAMS,
    }

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
        payload: dict[str, Any] = {
            "model": model.model_id,
            "messages": messages,
            # Ask OpenRouter for the sum it charged. It is the only direct check
            # on a registry price having gone stale.
            "usage": {"include": True},
        }
        for key in self._CHAT_PARAMS:
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
            usage_block = data.get("usage") if isinstance(data, dict) else None
            if choice0.get("finish_reason") == "length":
                raise truncated_error(
                    f"OpenRouter model {model.model_id}",
                    max_tokens=payload.get("max_tokens"),
                    completion_tokens=(usage_block or {}).get("completion_tokens"),
                )
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

        raw_usage = data.get("usage") if isinstance(data, dict) else None
        return RouterResponse(
            task=request.task,
            provider=self.name,
            model_id=model.model_id,
            usage=usage,
            text=text or None,
            attachments=attachments,
            raw_meta={
                "finish_reason": choice0.get("finish_reason"),
                "provider_request_id": data.get("id") if isinstance(data, dict) else None,
                # Kept beside the registry-based figure rather than replacing it:
                # the difference between the two is the staleness signal.
                "provider_cost_usd": self._reported_cost(raw_usage),
            },
        )

    # --- catalog ---------------------------------------------------------
    async def list_catalog(self) -> list[RegistryModel] | None:
        """Live model list with pricing (the source RegistryService used to own)."""
        if not self.is_configured():
            return None
        response = await self._request_with_retry("GET", "/models")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("OpenRouter returned invalid JSON") from exc
        raw_models = payload.get("data", []) if isinstance(payload, dict) else []
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        models: list[RegistryModel] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            model = self._parse_catalog_model(item, checked_at)
            if model is not None:
                models.append(model)
        return models

    @staticmethod
    def _to_float(value: Any) -> float:
        if value is None:
            return 0.0
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError):
            return 0.0

    @staticmethod
    def _extract_provider(model_id: str) -> str:
        if "/" in model_id:
            return model_id.split("/", 1)[0]
        return "other"

    @staticmethod
    def _media_set(raw: Any) -> set[str]:
        """`{"text", "image", ...}` out of one of OpenRouter's modality lists."""
        found: set[str] = set()
        for item in raw or []:
            value = str(item).lower()
            for medium in ("text", "image", "audio", "video", "file"):
                if medium in value:
                    found.add(medium)
        return found

    @classmethod
    def _extract_tasks(cls, raw: dict[str, Any]) -> tuple[Task, ...]:
        """The tasks a catalog entry can serve, from its input and output media.

        This used to read the media and answer in *billing groups*, which meant
        every vision chat model in the catalog came back tagged `image` — because
        `image` appeared in its inputs. Those rows then joined the candidate pool
        for image generation and were priced at `image_price`, which for a chat
        model is zero. So the direction of the mapping matters: a medium on the
        *input* side can only ever produce a `*-to-text` task here, because
        /chat/completions answers with text unless the model is one of the few
        that emits an image.
        """
        architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
        inputs = cls._media_set(architecture.get("input_modalities"))
        outputs = cls._media_set(architecture.get("output_modalities"))
        # The older `architecture.modality` field is a string like "text+image->text".
        legacy = str(architecture.get("modality") or "").lower()
        if "->" in legacy:
            left, _, right = legacy.partition("->")
            inputs |= cls._media_set(left.split("+"))
            outputs |= cls._media_set(right.split("+"))

        tasks: list[Task] = []
        if "image" in outputs:
            tasks.append(Task.TEXT_TO_IMAGE)
        # Text out, or nothing declared at all: this is a chat model, and the
        # catalog has plenty of entries with an empty architecture block.
        if "text" in outputs or not outputs:
            tasks.append(Task.TEXT_TO_TEXT)
            if "image" in inputs:
                tasks.append(Task.IMAGE_TO_TEXT)
        if not tasks:
            tasks.append(Task.TEXT_TO_TEXT)
        return tuple(tasks)

    @classmethod
    def _parse_catalog_model(
        cls, raw: dict[str, Any], checked_at: str | None = None
    ) -> RegistryModel | None:
        model_id = str(raw.get("id") or "").strip()
        if not model_id:
            return None
        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
        prompt_price = cls._to_float(pricing.get("prompt"))
        completion_price = cls._to_float(pricing.get("completion"))
        image_price = cls._to_float(pricing.get("image"))
        try:
            context_length = int(raw.get("context_length") or 0)
        except (ValueError, TypeError):
            context_length = 0
        tasks = cls._extract_tasks(raw)
        caps: list[str] = []
        if Task.IMAGE_TO_TEXT in tasks:
            caps.append("vision")
        return RegistryModel(
            service_provider_id="openrouter",
            model_provider=cls._extract_provider(model_id),
            model_id=model_id,
            display_name=str(raw.get("name") or model_id),
            # Groups are derived from the tasks, so a vision chat model is no
            # longer tagged as an image producer.
            modalities=(),
            tasks=tasks,
            capabilities=tuple(caps),
            tier=RegistryModel.compute_tier(prompt_price, completion_price),
            prompt_price=prompt_price,
            completion_price=completion_price,
            image_price=image_price,
            context_length=context_length,
            source="catalog",
            is_active=True,
            price_source=PRICE_SOURCE,
            price_source_kind="api",
            price_checked_at=checked_at,
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
        details = openai_shaped_usage(raw)
        return Usage(
            prompt_tokens=raw.get("prompt_tokens"),
            completion_tokens=raw.get("completion_tokens"),
            total_tokens=raw.get("total_tokens"),
            cache_read_tokens=details["cache_read"],
            reasoning_tokens=details["reasoning"],
            raw=raw,
        )

    @staticmethod
    def _reported_cost(raw: Any) -> float | None:
        """The sum OpenRouter itself charged, when it told us.

        Requested via `usage: {include: true}`. It is the best available check on
        a registry price going stale: the console shows both and highlights the
        gap (§7.4).
        """
        if not isinstance(raw, dict):
            return None
        value = raw.get("cost")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

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
