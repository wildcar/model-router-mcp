"""OpenRouter adapter (chat + image-in-chat).

Ports the response-parsing helpers from ort_bot/services/openrouter.py
(text extraction, image-output collection) and adds token-usage capture, which
the original `chat_completion` discards. The HTTP retry/error handling comes
from RetryClientMixin.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from router_mcp.adapters.base import (
    ProviderAdapter,
    RetryClientMixin,
    openai_shaped_usage,
    provider_error_for,
    truncated_error,
)
from router_mcp.domain import progress
from router_mcp.domain.errors import (
    ContentFiltered,
    ModelUnavailable,
    ProviderError,
    ProviderUnavailable,
)
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

IMAGE_HTTP_RE = re.compile(r"^https?://.+\.(?:png|jpe?g|webp|gif)(?:\?.*)?$", re.IGNORECASE)

# OpenRouter is the one provider whose catalog carries prices, so rows it
# produces are the only ones marked as API-sourced.
PRICE_SOURCE = "api:openrouter GET /models (pricing)"

# OpenRouter's server-side web search: the current server tool and the legacy
# plugin it superseded (both from ort_bot/services/openrouter.py, where the
# search ladder lived before it moved here).
WEB_SEARCH_TOOL_TYPE = "openrouter:web_search"
LEGACY_WEB_PLUGIN_ID = "web"

# "No endpoints found that support tool use" — OpenRouter's HTTP 404 when the
# pinned model has no tool-capable endpoint. Not transient, but exactly the
# case the legacy plugin covers: the plugin injects search results server-side
# and asks nothing of the model. Matched on the word because the wording is the
# provider's, not ours; on the tool rung an error naming tools is about the
# tool — the payload contains nothing else unusual.
TOOL_UNSUPPORTED_RE = re.compile(r"\btools?\b", re.IGNORECASE)

# Some image models answer only on OpenRouter's dedicated images endpoint
# (POST /images: model + prompt in, base64 out) and are absent from GET /models
# entirely — meta/muse-image was the first. A registry row opts in with this
# capability; a row that lacks it still lands there when the chat endpoint
# answers its distinctive 404 («…cannot be used with the chat/completions
# endpoint. Use the /api/v1/images endpoint instead»), matched below.
IMAGES_API_CAPABILITY = "images_api"
IMAGES_ONLY_RE = re.compile(r"images endpoint", re.IGNORECASE)
# Tasks the images endpoint can serve; also the only tasks whose 404 may mean
# "wrong endpoint" rather than "model gone".
IMAGE_TASKS = (Task.TEXT_TO_IMAGE, Task.IMAGE_TO_IMAGE)



def _escape_non_ascii(value: str) -> str:
    """Percent-encode only the characters a header cannot carry.

    Not `quote(value)`: that would also escape the ASCII punctuation — the
    colon and slashes of a URL, the bang of a title — and mangle exactly the
    part that was already fine.
    """
    return "".join(ch if ord(ch) < 128 else quote(ch) for ch in value)


def _referer_safe(value: str) -> str:
    """A caller-supplied URL as the HTTP-Referer value.

    Header values are ASCII; a Cyrillic URL would make httpx raise before the
    request ever left. Percent-encoding is the native representation of
    non-ASCII in a URL (browsers do the same in their own Referer), so it is
    the right escape here — and only here, not for the human-readable title.
    """
    return _escape_non_ascii(value.strip())


# Cyrillic to Latin for X-Title, one practical romanization. OpenRouter shows
# the header verbatim: a percent-encoded «Проверка» reached its usage dashboard
# as %D0%9F… (seen live, 2026-08-26), so the title must stay readable letters.
# Includes the Kazakh letters — the apps behind this router are Kazakhstani.
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ә": "a", "ғ": "g", "қ": "q", "ң": "n", "ө": "o", "ұ": "u", "ү": "u",
    "һ": "h", "і": "i",
}


def _title_safe(value: str) -> str:
    """A caller-supplied app name as the X-Title value, kept readable.

    ASCII passes through. Cyrillic is transliterated («ИИшная» → IIshnaya) —
    the dashboard on the other end prints the header as it came, so an escape
    a human cannot read defeats the point of naming the app. Anything still
    non-ASCII after that (another script entirely) falls back to
    percent-encoding, which at least survives the wire. The log keeps the
    original either way.
    """
    value = value.strip()
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        pass
    chars = []
    for ch in value:
        mapped = _CYR_TO_LAT.get(ch.lower())
        if mapped is None:
            chars.append(ch)
        else:
            chars.append(mapped.capitalize() if ch.isupper() else mapped)
    return _escape_non_ascii("".join(chars))


def _image_part(att: Attachment) -> dict[str, Any] | None:
    """One image as an OpenAI-shaped content part, or None if it is not an image.

    Inline as a data URI: the provider fetches nothing, so nothing has to be
    publicly reachable for a chat with a picture to work.
    """
    if att.kind != "image":
        return None
    if att.url:
        return {"type": "image_url", "image_url": {"url": att.url}}
    if att.data is None:
        return None
    encoded = base64.b64encode(att.data).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{att.mime};base64,{encoded}"}}


class OpenRouterAdapter(RetryClientMixin, ProviderAdapter):
    name = "openrouter"
    env_var = "OPENROUTER_API_KEY"
    # Chat-shaped models — including the ones that answer with an image — go
    # through /chat/completions. Models carrying `images_api` (and the ones the
    # chat endpoint refuses with its "use /api/v1/images" 404) go to the
    # dedicated images endpoint, which is also what makes editing an image
    # (`image-to-image`, reference photos in `input_references`) reachable here.
    supported_tasks = {
        Task.TEXT_TO_TEXT, Task.IMAGE_TO_TEXT, Task.TEXT_TO_IMAGE,
        Task.IMAGE_TO_IMAGE,
    }
    # A market of four hundred ids, five of which were ever addressed from here.
    # Fetched on demand, stored on adoption or on a confirmed answer (ADR-0008).
    catalog_scope = "browse"
    _CHAT_PARAMS = ("temperature", "max_tokens", "top_p", "reasoning")
    # What POST /images reads. Wire names as OpenRouter spells them; each path
    # builds its payload from its own tuple, so a chat knob sent to an images
    # model is simply not sent on (and vice versa) — the task-level declaration
    # below is the union, because one task can travel either path.
    _IMAGE_PARAMS = ("aspect_ratio", "resolution", "n", "quality", "output_format", "seed")
    # `web_search` is normalized, not a passthrough field: the caller says a
    # boolean, this adapter translates it into whatever OpenRouter accepts
    # today. That keeps MCP clients unaware of OpenRouter's tool vocabulary and
    # leaves room for other providers to honour the same flag later.
    SUPPORTED_PARAMS = {
        Task.TEXT_TO_TEXT: _CHAT_PARAMS + ("web_search",),
        Task.IMAGE_TO_TEXT: _CHAT_PARAMS + ("web_search",),
        Task.TEXT_TO_IMAGE: _CHAT_PARAMS + _IMAGE_PARAMS,
        Task.IMAGE_TO_IMAGE: _IMAGE_PARAMS,
    }

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        timeout_seconds: float = 60.0,
        retries: int = 2,
        app_url: str = "https://mr.wildcar.org",
        app_name: str = "model-router-mcp",
    ) -> None:
        self._api_key = api_key
        self._retries = retries
        # Who the request says it is from, when the caller does not: OpenRouter
        # attributes traffic by HTTP-Referer / X-Title, so these are set per
        # request (not as client defaults — a default X-Title cannot be unset
        # by a merge, and it would mislabel an app identified by URL alone).
        self._app_url = app_url
        self._app_name = app_name
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

    def _app_headers(self, request: RouterRequest | None = None) -> dict[str, str]:
        """The identity headers for one call: the caller's app, or ourselves.

        The site is the identity of record, so `app_url` lands in HTTP-Referer
        and a request that carries neither field goes out as the router itself.
        X-Title is set only when there is a real name to put in it: an app
        identified by URL alone must not be titled with our name.
        """
        app_url = request.app_url if request else None
        app_name = request.app_name if request else None
        headers = {"HTTP-Referer": _referer_safe(app_url or self._app_url)}
        title = app_name or (self._app_name if not app_url else None)
        if title:
            title = _title_safe(title)
        if title:
            headers["X-Title"] = title
        return headers

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        if request.task in IMAGE_TASKS and IMAGES_API_CAPABILITY in model.capabilities:
            return await self._generate_image(request, model)
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

        # Only for tasks that declare it: on text-to-image the flag stays out of
        # the payload and the router names it in `ignored_params` instead.
        web_search = bool(request.params.get("web_search")) and "web_search" in (
            self.SUPPORTED_PARAMS.get(request.task) or ()
        )
        headers = self._app_headers(request)
        try:
            if web_search:
                data, search_mode = await self._chat_with_web_search(
                    payload, model.model_id, headers
                )
            else:
                data, search_mode = await self._chat_data(payload, headers), None
        except ModelUnavailable as exc:
            # The provider's own hint that this is an images-endpoint model a
            # row simply has not been marked for — not a retired id. Rows adopt
            # the `images_api` capability by hand; this keeps an unmarked one
            # working instead of failing on a distinction the caller cannot see.
            if request.task in IMAGE_TASKS and IMAGES_ONLY_RE.search(str(exc)):
                progress.emit(
                    "endpoint",
                    "модель отвечает только на /images, повторяю туда",
                )
                return await self._generate_image(request, model)
            raise

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
            if choice0.get("finish_reason") == "content_filter":
                raise ContentFiltered(
                    f"OpenRouter model {model.model_id} withheld the answer "
                    "(finish_reason=content_filter)"
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
        raw_meta: dict[str, Any] = {
            "finish_reason": choice0.get("finish_reason"),
            "provider_request_id": data.get("id") if isinstance(data, dict) else None,
            # Kept beside the registry-based figure rather than replacing it:
            # the difference between the two is the staleness signal.
            "provider_cost_usd": self._reported_cost(raw_usage),
        }
        if web_search:
            # What was asked for and what actually ran. The ladder may have
            # answered without search at all, and the caller must not learn
            # that by noticing the answer cites nothing.
            raw_meta["web_search"] = True
            raw_meta["web_search_mode"] = search_mode
        return RouterResponse(
            task=request.task,
            provider=self.name,
            model_id=model.model_id,
            usage=usage,
            text=text or None,
            attachments=attachments,
            raw_meta=raw_meta,
        )

    async def _generate_image(
        self, request: RouterRequest, model: RegistryModel
    ) -> RouterResponse:
        """POST /images: the endpoint for models the chat surface refuses.

        Prompt plus reference images in, base64 images out. The reference
        images ride in `input_references` in the same content-part shape the
        chat endpoint uses, so `_image_part` serves both. The create is
        synchronous and billed on success, like a chat call, so the standard
        retry policy applies.
        """
        payload: dict[str, Any] = {"model": model.model_id}
        if request.text:
            payload["prompt"] = request.text
        for key in self._IMAGE_PARAMS:
            if key in request.params:
                payload[key] = request.params[key]
        references = [
            part for part in (_image_part(att) for att in request.attachments) if part
        ]
        if references:
            payload["input_references"] = references

        response = await self._request_with_retry(
            "POST",
            "/images",
            json=payload,
            headers=self._app_headers(request),
            # Generation regularly outlives the 60-second chat default; the
            # same allowance the codex image path gets (nginx already waits).
            timeout=180.0,
        )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("OpenRouter returned invalid JSON") from exc
        top_error = self._extract_top_level_error(data)
        if top_error is not None:
            message, code = top_error
            if code in (429, 500, 502, 503, 504):
                raise ProviderUnavailable(f"OpenRouter temporarily unavailable: {message}")
            raise provider_error_for(message, provider_status=code if isinstance(code, int) else None)

        attachments: list[Attachment] = []
        entries = data.get("data") if isinstance(data, dict) else None
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            mime = str(item.get("media_type") or "image/png")
            b64 = item.get("b64_json")
            if isinstance(b64, str) and b64.strip():
                attachments.append(
                    Attachment(kind="image", mime=mime, data=base64.b64decode(b64))
                )
            elif isinstance(item.get("url"), str) and item["url"].strip():
                attachments.append(Attachment(kind="image", mime=mime, url=item["url"]))
        if not attachments:
            logger.warning(
                "OpenRouter /images parse miss model=%s sample=%s",
                model.model_id,
                json.dumps(data, ensure_ascii=False)[:600],
            )
            raise ProviderError("Model returned an empty response")

        raw_usage = data.get("usage") if isinstance(data, dict) else None
        raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
        usage = Usage(
            prompt_tokens=raw_usage.get("prompt_tokens"),
            completion_tokens=raw_usage.get("completion_tokens"),
            total_tokens=raw_usage.get("total_tokens"),
            images=len(attachments),
            raw=raw_usage,
        )
        return RouterResponse(
            task=request.task,
            provider=self.name,
            model_id=model.model_id,
            usage=usage,
            attachments=attachments,
            raw_meta={
                "provider_request_id": data.get("id") if isinstance(data, dict) else None,
                "provider_cost_usd": self._reported_cost(raw_usage),
                "endpoint": "/images",
            },
        )

    async def _chat_data(
        self, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """POST /chat/completions and return the parsed body.

        `headers` carries the app identity of the request (HTTP-Referer /
        X-Title); the client itself holds none, so what one call says about its
        origin cannot leak into the next.

        Transient failures — 429/5xx (also when reported as a top-level `error`
        object in a 200 body), timeouts, invalid JSON — surface as
        `ProviderUnavailable`; everything else is `ProviderError`/`AccessDenied`
        from the retry mixin.
        """
        response = await self._request_with_retry(
            "POST", "/chat/completions", json=payload, headers=headers
        )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("OpenRouter returned invalid JSON") from exc

        top_error = self._extract_top_level_error(data)
        if top_error is not None:
            message, code = top_error
            if code in (429, 500, 502, 503, 504):
                raise ProviderUnavailable(f"OpenRouter temporarily unavailable: {message}")
            raise provider_error_for(message, provider_status=code if isinstance(code, int) else None)
        return data if isinstance(data, dict) else {}

    async def _chat_with_web_search(
        self,
        payload: dict[str, Any],
        model_id: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """The search ladder: server tool → legacy plugin → no search.

        Restored from ort_bot's `chat_completion`. A rung falls through on
        `ProviderUnavailable` — the transient failures `_chat_data` reserves for
        "try again later" — and the tool rung additionally on the 404 that says
        the model has no tool-capable endpoint (checked live: ui-tars answers
        exactly that, and the plugin then works). `AccessDenied` and every other
        `ProviderError` propagate: a bad key, a rejected prompt or a refusal
        would fail identically without search, and a retry would only mask
        which request the provider refused.

        Returns the parsed body and the mode that actually answered
        ("server_tool" | "plugin" | "off"); the progress events land in the run
        timeline, which is how the journal keeps the mode.
        """
        try:
            data = await self._chat_data(
                {**payload, "tools": [{"type": WEB_SEARCH_TOOL_TYPE}]}, headers
            )
            progress.emit("web_search", "веб-поиск: серверный инструмент")
            return data, "server_tool"
        except ProviderUnavailable as exc:
            logger.warning(
                "OpenRouter web_search server tool unavailable model=%s (%s); "
                "retrying with legacy web plugin", model_id, exc,
            )
            progress.emit("web_search", "серверный поиск недоступен, пробую плагин web")
        except ProviderError as exc:
            if not TOOL_UNSUPPORTED_RE.search(str(exc)):
                raise
            logger.warning(
                "OpenRouter model has no tool-use endpoint model=%s (%s); "
                "retrying with legacy web plugin", model_id, exc,
            )
            progress.emit("web_search", "модель без поддержки tools, пробую плагин web")
        try:
            data = await self._chat_data(
                {**payload, "plugins": [{"id": LEGACY_WEB_PLUGIN_ID}]}, headers
            )
            progress.emit("web_search", "веб-поиск: плагин web")
            return data, "plugin"
        except ProviderUnavailable as exc:
            logger.warning(
                "OpenRouter legacy web plugin unavailable model=%s (%s); "
                "retrying without web search", model_id, exc,
            )
            progress.emit("web_search", "плагин web недоступен, выполняю запрос без поиска")
        data = await self._chat_data(payload, headers)
        progress.emit("web_search", "веб-поиск не выполнялся, ответ без поиска")
        return data, "off"

    # --- catalog ---------------------------------------------------------
    async def list_catalog(self) -> list[RegistryModel] | None:
        """Live model list with pricing (the source RegistryService used to own)."""
        if not self.is_configured():
            return None
        # The catalog is the router's own request, so it identifies as ourselves.
        response = await self._request_with_retry(
            "GET", "/models", headers=self._app_headers()
        )
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
        # USD per server-side web search call, billed on top of the tokens.
        # Published for the models whose provider prices native search (~150 of
        # the catalog); absent means the server tool is not separately billed.
        web_search_price = cls._to_float(pricing.get("web_search"))
        # Cache rates. Left unparsed, every cached prompt token was billed at
        # the assumed full prompt rate and the whole figure read `estimated` —
        # prod run 10242 came out $0.0172 against the provider's $0.0147, and
        # with these two rates the arithmetic matches the charge to the cent.
        cache_read_price = cls._to_float(pricing.get("input_cache_read"))
        cache_write_price = cls._to_float(pricing.get("input_cache_write"))
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
            web_search_price=web_search_price,
            cache_read_price=cache_read_price,
            cache_write_price=cache_write_price,
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
        """The OpenAI-shaped message list, with images as content parts.

        Attachments used to be dropped here without a word, so a vision request
        reached the model as its text alone and came back "send me an image".
        Declaring `image-to-text` and then not sending the image is the exact
        failure ADR-0006 exists to prevent, one level down.
        """
        images = [
            part for part in (_image_part(att) for att in request.attachments) if part
        ]
        if request.messages:
            messages = [dict(m) for m in request.messages]
            if images:
                # Onto the last user turn, which is what the images are about.
                for message in reversed(messages):
                    if message.get("role") in (None, "user"):
                        content = message.get("content")
                        parts = (
                            [{"type": "text", "text": content}]
                            if isinstance(content, str)
                            else list(content or [])
                        )
                        message["content"] = parts + images
                        break
                else:
                    messages.append({"role": "user", "content": images})
            return messages
        if images:
            parts: list[dict[str, Any]] = []
            if request.text:
                parts.append({"type": "text", "text": request.text})
            return [{"role": "user", "content": parts + images}]
        if request.text is not None:
            return [{"role": "user", "content": request.text}]
        raise ProviderError("Request has neither `text` nor `messages`")

    @staticmethod
    def _parse_usage(raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        details = openai_shaped_usage(raw)
        # How many server-side searches the request actually ran — the billable
        # count behind `web_search_price` (two searches in one answer is
        # routine). OpenRouter-specific shape, hence parsed here and not in
        # `openai_shaped_usage`.
        tool_use = raw.get("server_tool_use_details")
        searches = tool_use.get("web_search_requests") if isinstance(tool_use, dict) else None
        return Usage(
            prompt_tokens=raw.get("prompt_tokens"),
            completion_tokens=raw.get("completion_tokens"),
            total_tokens=raw.get("total_tokens"),
            cache_read_tokens=details["cache_read"],
            reasoning_tokens=details["reasoning"],
            web_searches=int(searches) if searches else None,
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
