"""Codex OAuth adapter — image generation via the ChatGPT backend.

Ported from ort_bot/services/codex_image.py: loads an OAuth auth.json (flat or
providers.openai-codex layout), streams a /responses image_generation call, and
refreshes the access token on 401/403. Returns the generated PNG as bytes so the
router persists it to the blob store.

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


class CodexImageAdapter(ProviderAdapter):
    name = "codex-oauth"
    env_var = "CODEX_AUTH_PATH"
    # Generation only, on purpose. The same backend answers text prompts and
    # reads images, and that branch is a separate piece of work; declaring the
    # capability before the code exists would lie to the router. Editing an
    # existing image it cannot do at all, so `image-to-image` will never appear
    # here and a request for it gets a clear refusal instead of quietly landing
    # on gpt-image-2 (ADR-0006).
    supported_tasks = {Task.TEXT_TO_IMAGE}
    # Size, quality and both model ids are fixed by configuration; the caller
    # only supplies the prompt.
    SUPPORTED_PARAMS = {Task.TEXT_TO_IMAGE: ()}

    def __init__(
        self,
        auth_path: str | None,
        base_url: str,
        main_model: str,
        image_model: str,
        size: str,
        quality: str,
        timeout_seconds: float = 180.0,
        retries: int = 1,
    ) -> None:
        self._auth_path = Path(auth_path).expanduser() if auth_path else None
        self._base_url = base_url.rstrip("/")
        self._main_model = main_model
        self._image_model = image_model
        self._size = size
        self._quality = quality
        self._retries = retries
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    def is_configured(self) -> bool:
        return self._auth_path is not None and self._auth_path.exists()

    async def close(self) -> None:
        await self._client.aclose()

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        prompt = (request.text or "").strip()
        if not prompt:
            raise ProviderError("generate_image requires a non-empty `prompt`")
        tokens = self._load_tokens()
        try:
            data_uri = await self._generate_with_tokens(prompt, tokens)
        except AccessDenied:
            refreshed = await self._recover_tokens(tokens)
            data_uri = await self._generate_with_tokens(prompt, refreshed)

        raw_b64 = data_uri.split(",", 1)[1]
        image_bytes = base64.b64decode(raw_b64)
        return RouterResponse(
            task=Task.TEXT_TO_IMAGE,
            provider=self.name,
            model_id=model.model_id or self._image_model,
            usage=Usage(images=1, raw={"size": self._size, "quality": self._quality}),
            attachments=[Attachment(kind="image", mime="image/png", data=image_bytes)],
        )

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

    def _payload(self, prompt: str) -> dict[str, Any]:
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
                    "size": self._size,
                    "quality": self._quality,
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

    async def _generate_with_tokens(self, prompt: str, tokens: dict[str, str]) -> str:
        access_token = tokens["access_token"]
        account_id = tokens.get("account_id")
        payload = self._payload(prompt)
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
                                f"Codex image backend unavailable: HTTP {response.status_code}"
                            )
                        continue
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        raise ProviderError(
                            f"Codex image backend error HTTP {response.status_code}: {body[:500]}"
                        )
                    image_b64 = await self._extract_image_b64_from_stream(response)
                    if image_b64:
                        return f"data:image/png;base64,{image_b64}"
                    raise ProviderError("Codex image backend returned no image")
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt > self._retries:
                    raise ProviderUnavailable("Could not reach Codex image backend") from exc
                continue
        status = response.status_code if response is not None else "unknown"
        raise ProviderUnavailable(f"Codex image backend unavailable: {status}")

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

    async def _extract_image_b64_from_stream(self, response: httpx.Response) -> str | None:
        latest_partial: str | None = None
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
            event_type = event.get("type") if isinstance(event, dict) else None
            if event_type == "error":
                message = event.get("message") if isinstance(event, dict) else None
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
