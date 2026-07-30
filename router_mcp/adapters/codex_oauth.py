"""Codex OAuth adapter — text, vision and image generation via the ChatGPT backend.

One credential, one endpoint (`/responses`), three tasks:

* `text-to-text` and `image-to-text` — a plain streamed answer, no tools;
* `text-to-image` — the same call with the `image_generation` tool required.

It used to declare only image generation, which is where the taxonomy work
started: the provider was tagged `{IMAGE}` because it can produce a picture, and
that single fact said nothing about it also *reading* pictures and nothing about
it being unable to *edit* one. `image-to-image` is still absent from
`supported_tasks` — this backend cannot do it, and the router now refuses such a
request instead of quietly sending it to gpt-image-2 and returning a brand-new
image (ADR-0006).

Loads an OAuth auth.json (flat or providers.openai-codex layout) and refreshes
the access token on 401/403. Binary results come back as bytes so the router
persists them to the blob store.

NOTE: the OAuth refresh token is single-use and rotates on refresh, and OpenAI
revokes the whole token family when a rotated-away token is reused (see
openai-auth-sync ADR-0002) — so never keep copies of one credential. Point
CODEX_AUTH_PATH at the codex CLI's own auth.json of the SAME user: the file is
re-read per request and re-checked before refreshing, so both writers stay on
one rotation chain.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.domain.errors import AccessDenied, NotConfigured, ProviderError, ProviderUnavailable
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_REFRESH_URL = "https://auth.openai.com/oauth/token"
# Where the rates live for anyone who does pay per token. The subscription does
# not publish any, which is why catalog rows come back `unknown`.
CODEX_PRICING_PAGE = "https://platform.openai.com/docs/pricing"


class CodexOAuthAdapter(ProviderAdapter):
    name = "codex-oauth"
    env_var = "CODEX_AUTH_PATH"
    # No `image-to-image`: this backend cannot change a picture it is given, and
    # that absence is the whole point of declaring tasks rather than media.
    supported_tasks = {Task.TEXT_TO_TEXT, Task.IMAGE_TO_TEXT, Task.TEXT_TO_IMAGE}
    _TEXT_PARAMS = ("instructions", "reasoning_effort")
    SUPPORTED_PARAMS = {
        Task.TEXT_TO_TEXT: _TEXT_PARAMS,
        Task.IMAGE_TO_TEXT: _TEXT_PARAMS,
        # `size` and `quality` used to be reachable only through the environment,
        # so the console described a fixed picture it could not change. The env
        # values remain the defaults.
        Task.TEXT_TO_IMAGE: ("size", "quality"),
    }

    def __init__(
        self,
        auth_path: str | None,
        base_url: str,
        main_model: str,
        image_model: str,
        size: str,
        quality: str,
        client_version: str = "0.146.0",
        timeout_seconds: float = 180.0,
        retries: int = 1,
    ) -> None:
        self._auth_path = Path(auth_path).expanduser() if auth_path else None
        self._base_url = base_url.rstrip("/")
        self._main_model = main_model
        self._image_model = image_model
        self._size = size
        self._quality = quality
        self._client_version = client_version
        self._retries = retries
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    def is_configured(self) -> bool:
        return self._auth_path is not None and self._auth_path.exists()

    async def close(self) -> None:
        await self._client.aclose()

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        if request.task is Task.TEXT_TO_IMAGE:
            return await self._generate_image(request, model)
        return await self._generate_text(request, model)

    # --- text and vision -------------------------------------------------
    async def _generate_text(
        self, request: RouterRequest, model: RegistryModel
    ) -> RouterResponse:
        payload = self._text_payload(request, model)
        result = await self._with_recovery(payload, self._read_text_stream)
        text, usage_raw = result
        if not text.strip():
            raise ProviderError(
                f"Codex model {model.model_id or self._main_model} returned no text"
            )
        return RouterResponse(
            task=request.task,
            provider=self.name,
            model_id=model.model_id or self._main_model,
            usage=self._parse_usage(usage_raw),
            text=text,
            raw_meta={"provider_request_id": (usage_raw or {}).get("response_id")},
        )

    def _text_payload(self, request: RouterRequest, model: RegistryModel) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        prompt = (request.text or "").strip()
        if prompt:
            content.append({"type": "input_text", "text": prompt})
        # Images travel as data URIs, which is the only form `/responses` takes
        # inline; the router has already loaded the bytes from the blob store.
        for att in request.attachments:
            if att.kind != "image":
                continue
            uri = att.url or self._data_uri(att)
            if uri:
                content.append({"type": "input_image", "image_url": uri})
        if not content:
            raise ProviderError("Request carries neither text nor an image")

        payload: dict[str, Any] = {
            "model": model.model_id or self._main_model,
            "store": False,
            "stream": True,
            "input": self._history(request) + [
                {"type": "message", "role": "user", "content": content}
            ],
        }
        instructions = request.params.get("instructions")
        if instructions:
            payload["instructions"] = str(instructions)
        effort = request.params.get("reasoning_effort")
        if effort:
            payload["reasoning"] = {"effort": str(effort)}
        return payload

    @staticmethod
    def _history(request: RouterRequest) -> list[dict[str, Any]]:
        """Earlier turns, when the caller sent a history rather than one prompt."""
        out: list[dict[str, Any]] = []
        for message in request.messages or []:
            role = str(message.get("role") or "user")
            text = message.get("content")
            if not isinstance(text, str) or not text.strip():
                continue
            kind = "output_text" if role == "assistant" else "input_text"
            out.append(
                {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}
            )
        return out

    @staticmethod
    def _data_uri(att: Attachment) -> str | None:
        if not att.data:
            return None
        return f"data:{att.mime};base64,{base64.b64encode(att.data).decode()}"

    @staticmethod
    def _parse_usage(raw: dict[str, Any] | None) -> Usage:
        """`/responses` reports `input_tokens`/`output_tokens`, with the cached
        part *inside* the input total — which is the contract `Usage` wants, so
        the cache count is recorded without adjusting the prompt figure."""
        block = (raw or {}).get("usage") or {}
        details = block.get("input_tokens_details") or {}
        return Usage(
            prompt_tokens=block.get("input_tokens"),
            completion_tokens=block.get("output_tokens"),
            total_tokens=block.get("total_tokens"),
            cache_read_tokens=details.get("cached_tokens"),
            reasoning_tokens=(block.get("output_tokens_details") or {}).get(
                "reasoning_tokens"
            ),
            raw=dict(block),
        )

    async def _read_text_stream(self, response: httpx.Response) -> tuple[str, dict[str, Any]]:
        """Assemble the answer from the delta events, and keep the final usage.

        Deltas are joined with no separator: they are token fragments, and a
        newline between them would mangle every answer.
        """
        chunks: list[str] = []
        final: dict[str, Any] = {}
        async for event in self._events(response):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    chunks.append(delta)
            elif kind in ("response.completed", "response.incomplete"):
                payload = event.get("response") or {}
                final = {
                    "usage": payload.get("usage") or {},
                    "response_id": payload.get("id"),
                    "status": payload.get("status"),
                }
            elif kind == "error":
                raise ProviderError(str(event.get("message") or "Codex stream error"))
        if not chunks:
            # Some models answer in one piece rather than in deltas.
            text = self._text_from_output(final)
            if text:
                chunks.append(text)
        return "".join(chunks), final

    @staticmethod
    def _text_from_output(final: dict[str, Any]) -> str:
        parts: list[str] = []
        for item in (final.get("output") or []):
            for piece in (item or {}).get("content") or []:
                value = (piece or {}).get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)

    # --- image generation ------------------------------------------------
    async def _generate_image(
        self, request: RouterRequest, model: RegistryModel
    ) -> RouterResponse:
        prompt = (request.text or "").strip()
        if not prompt:
            raise ProviderError("generate_image requires a non-empty `prompt`")
        size = str(request.params.get("size") or self._size)
        quality = str(request.params.get("quality") or self._quality)
        payload = self._payload(prompt, size=size, quality=quality)

        async def read(response: httpx.Response) -> str | None:
            return await self._extract_image_b64_from_stream(response)

        image_b64 = await self._with_recovery(payload, read)
        if not image_b64:
            raise ProviderError("Codex image backend returned no image")
        image_bytes = base64.b64decode(image_b64)
        return RouterResponse(
            task=Task.TEXT_TO_IMAGE,
            provider=self.name,
            model_id=model.model_id or self._image_model,
            usage=Usage(images=1, raw={"size": size, "quality": quality}),
            attachments=[Attachment(kind="image", mime="image/png", data=image_bytes)],
        )

    # --- catalog ---------------------------------------------------------
    async def list_catalog(self) -> list[RegistryModel] | None:
        """The models this ChatGPT account may actually address.

        The backend does publish a list — `GET /models?client_version=…`, the
        same one the codex CLI caches in `~/.codex/models_cache.json` — and it
        answers with six usable slugs, not the one the seed row held. It also
        names, per model, the reasoning levels that model accepts, which is the
        only place that fact exists: `/responses` rejects an unknown level with a
        400 and no list of models attached.

        No prices: a ChatGPT subscription does not publish per-token rates, so
        the rows come back `unknown` rather than free (a zero here would make
        them the cheapest thing in the registry and win every automatic pick).
        """
        if not self.is_configured():
            return None
        payload = await self._get_json("/models", {"client_version": self._client_version})
        raw = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise ProviderError("Codex model list has no `models` array")

        models: list[RegistryModel] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "").strip()
            if not slug or item.get("supported_in_api") is False:
                continue
            # `visibility: hide` is an internal model the client never offers
            # (codex-auto-review); listing it would put it in the picker.
            if str(item.get("visibility") or "list") != "list":
                continue
            media = {str(m).lower() for m in item.get("input_modalities") or []}
            tasks = [Task.TEXT_TO_TEXT]
            caps: list[str] = []
            if "image" in media:
                tasks.append(Task.IMAGE_TO_TEXT)
                caps.append("vision")
            # The per-model reasoning levels, stored as capabilities so the
            # console can offer exactly these and nothing else.
            for level in item.get("supported_reasoning_levels") or []:
                effort = str((level or {}).get("effort") or "").strip()
                if effort:
                    caps.append(f"effort:{effort}")
            models.append(
                RegistryModel(
                    service_provider_id=self.name,
                    model_provider="openai",
                    model_id=slug,
                    display_name=str(item.get("display_name") or slug),
                    tasks=tuple(tasks),
                    capabilities=tuple(caps),
                    context_length=self._int(item.get("context_window")),
                    tier="expensive",
                    source="catalog",
                    is_active=True,
                    price_source=CODEX_PRICING_PAGE,
                    price_source_kind="unknown",
                )
            )
        # The image tool is not in that list — it is a tool `/responses` calls,
        # not a model the account chats with — but it is addressable, so it
        # belongs in the answer. Without it every sync would retire the row that
        # `generate_image` runs on.
        models.append(
            RegistryModel(
                service_provider_id=self.name,
                model_provider="openai",
                model_id=self._image_model,
                display_name="Codex image generation",
                tasks=(Task.TEXT_TO_IMAGE,),
                tier="expensive",
                source="catalog",
                is_active=True,
                price_source=CODEX_PRICING_PAGE,
                price_source_kind="unknown",
            )
        )
        return models

    async def _get_json(self, path: str, params: dict[str, str]) -> Any:
        """One authenticated GET, retried once on an expired token.

        Same recovery as `_with_recovery` and for the same reason — the refresh
        token is single-use — but this side of the adapter reads a plain JSON
        body rather than an SSE stream.
        """
        tokens = self._load_tokens()
        try:
            return await self._get_once(path, params, tokens)
        except AccessDenied:
            return await self._get_once(path, params, await self._recover_tokens(tokens))

    async def _get_once(self, path: str, params: dict[str, str], tokens: dict[str, str]) -> Any:
        response = await self._client.get(
            f"{self._base_url}{path}",
            params=params,
            headers=self._headers(tokens["access_token"], tokens.get("account_id")),
        )
        if response.status_code in (401, 403):
            raise AccessDenied("Codex OAuth access token is expired or rejected")
        if response.status_code in (429, 500, 502, 503, 504):
            raise ProviderUnavailable(
                f"Codex backend unavailable: HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"Codex backend error HTTP {response.status_code}: {response.text[:500]}"
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("Codex backend returned invalid JSON") from exc

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    # --- auth.json handling --------------------------------------------
    def _load_auth_data(self) -> dict[str, Any]:
        if self._auth_path is None or not self._auth_path.exists():
            raise NotConfigured(f"Codex auth file not found: {self._auth_path}")
        try:
            return json.loads(self._auth_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NotConfigured(f"Codex auth file is not valid JSON: {self._auth_path}") from exc

    def _load_tokens(self) -> dict[str, str]:
        data = self._load_auth_data()
        container = self._tokens_container(data)
        tokens = container.get("tokens") if container is not None else None
        if not isinstance(tokens, dict):
            raise NotConfigured("Codex auth file must contain providers.openai-codex.tokens or top-level tokens")
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise NotConfigured("Codex auth file does not contain access_token")
        return {str(k): str(v) for k, v in tokens.items() if isinstance(v, str)}

    @staticmethod
    def _tokens_container(data: dict[str, Any]) -> dict[str, Any] | None:
        providers = data.get("providers")
        if isinstance(providers, dict):
            oc = providers.get("openai-codex")
            if isinstance(oc, dict) and isinstance(oc.get("tokens"), dict):
                return oc
        if isinstance(data.get("tokens"), dict):
            return data
        return None

    @staticmethod
    def _extract_account_id(access_token: str) -> str | None:
        try:
            parts = access_token.split(".")
            if len(parts) < 2:
                return None
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
            auth = claims.get("https://api.openai.com/auth", {})
            acc = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
            return acc if isinstance(acc, str) and acc.strip() else None
        except Exception:  # noqa: BLE001
            return None

    def _headers(self, access_token: str, account_id: str | None) -> dict[str, str]:
        resolved = self._extract_account_id(access_token) or account_id
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "codex_cli_rs/0.0.0 (model-router-mcp)",
            "originator": "codex_cli_rs",
            "ChatGPT-Account-ID": resolved or "",
        }

    def _payload(self, prompt: str, *, size: str, quality: str) -> dict[str, Any]:
        return {
            "model": self._main_model,
            "store": False,
            "stream": True,
            "instructions": (
                "You are an assistant that must fulfill image generation requests by using "
                "the image_generation tool when provided."
            ),
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}
            ],
            "tools": [
                {
                    "type": "image_generation",
                    "model": self._image_model,
                    "size": size,
                    "quality": quality,
                    "output_format": "png",
                    "background": "opaque",
                    "partial_images": 1,
                }
            ],
            "tool_choice": {
                "type": "allowed_tools",
                "mode": "required",
                "tools": [{"type": "image_generation"}],
            },
        }

    async def _with_recovery(self, payload: dict[str, Any], read: Any) -> Any:
        """One `/responses` call, retried once on an expired token.

        The token dance is identical for text and for images, so it lives here
        rather than in each branch: two copies of a single-use-refresh-token
        recovery is two chances to break the rotation chain.
        """
        tokens = self._load_tokens()
        try:
            return await self._stream(payload, tokens, read)
        except AccessDenied:
            refreshed = await self._recover_tokens(tokens)
            return await self._stream(payload, refreshed, read)

    async def _stream(
        self, payload: dict[str, Any], tokens: dict[str, str], read: Any
    ) -> Any:
        access_token = tokens["access_token"]
        account_id = tokens.get("account_id")
        response: httpx.Response | None = None
        for attempt in range(1, self._retries + 2):
            try:
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/responses",
                    headers=self._headers(access_token, account_id),
                    json=payload,
                ) as response:
                    if response.status_code in (401, 403):
                        raise AccessDenied("Codex OAuth access token is expired or rejected")
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt > self._retries:
                            raise ProviderUnavailable(
                                f"Codex backend unavailable: HTTP {response.status_code}"
                            )
                        continue
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        raise ProviderError(
                            f"Codex backend error HTTP {response.status_code}: {body[:500]}"
                        )
                    return await read(response)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt > self._retries:
                    raise ProviderUnavailable("Could not reach the Codex backend") from exc
                continue
        status = response.status_code if response is not None else "unknown"
        raise ProviderUnavailable(f"Codex backend unavailable: {status}")

    async def _recover_tokens(self, failed: dict[str, str]) -> dict[str, str]:
        """After a 401/403, prefer tokens another writer (the codex CLI shares
        this file) already refreshed on disk; refresh ourselves only when the
        file still holds the token that just failed. This keeps the single-use
        refresh token from being reused across writers."""
        latest = self._load_tokens()
        if latest.get("access_token") != failed.get("access_token"):
            return latest
        return await self._refresh_tokens(latest)

    async def _refresh_tokens(self, tokens: dict[str, str]) -> dict[str, str]:
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise AccessDenied("Codex OAuth refresh_token is missing")
        response = await self._client.post(
            CODEX_TOKEN_REFRESH_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise AccessDenied(f"Codex OAuth token refresh failed: HTTP {response.status_code}")
        refreshed = response.json()
        new_tokens = dict(tokens)
        for key in ("access_token", "refresh_token", "id_token"):
            value = refreshed.get(key)
            if isinstance(value, str) and value.strip():
                new_tokens[key] = value
        self._save_tokens(new_tokens)
        return new_tokens

    def _save_tokens(self, tokens: dict[str, str]) -> None:
        data = self._load_auth_data()
        container = self._tokens_container(data)
        if container is None:
            container = data.setdefault("providers", {}).setdefault("openai-codex", {})
        container["tokens"] = tokens
        container["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert self._auth_path is not None
        self._auth_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    async def _events(response: httpx.Response) -> Any:
        """The SSE stream as parsed JSON objects. Shared by both branches, so the
        framing is understood in one place."""
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event

    async def _extract_image_b64_from_stream(self, response: httpx.Response) -> str | None:
        latest_partial: str | None = None
        async for event in self._events(response):
            event_type = event.get("type")
            if event_type == "error":
                message = event.get("message")
                raise ProviderError(str(message or "Codex image stream returned an error"))
            found = self._extract_image_b64(event)
            if found:
                if event_type == "response.image_generation_call.partial_image":
                    latest_partial = found
                else:
                    return found
        return latest_partial

    @classmethod
    def _extract_image_b64(cls, node: Any) -> str | None:
        if isinstance(node, dict):
            if node.get("type") == "image_generation_call":
                result = node.get("result")
                if isinstance(result, str) and result.strip():
                    return result.strip()
            for key in ("result", "b64_json", "image_b64", "partial_image_b64"):
                value = node.get(key)
                if isinstance(value, str) and value.strip() and cls._looks_like_base64_image(value):
                    return value.strip()
            for value in node.values():
                found = cls._extract_image_b64(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = cls._extract_image_b64(item)
                if found:
                    return found
        return None

    @staticmethod
    def _looks_like_base64_image(value: str) -> bool:
        stripped = value.strip()
        if stripped.startswith("data:image/"):
            return False
        return len(stripped) > 100 and all(c.isalnum() or c in "+/=_-" for c in stripped[:200])
