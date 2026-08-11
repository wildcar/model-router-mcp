"""Yandex SpeechKit adapter — text-to-speech.

TTS API v1 (`POST /speech/v1/tts:synthesize`): a form-encoded request that
answers with OGG/Opus bytes. Auth is a service-account Api-Key
(`YANDEX_API_KEY`); `folderId` is required only for IAM-token auth, so it is
sent only when configured. SpeechKit bills per character, same shape as
ElevenLabs: `usage.characters` carries the count, the registry's
`character_price` is the cost lever.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from router_mcp.adapters.base import ProviderAdapter, RetryClientMixin
from router_mcp.domain.errors import ProviderError
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

# Same text hygiene as the ElevenLabs adapter: markdown noise reads terribly.
_CLEAN_RE = [
    (re.compile(r"[*_`#>\[\]\(\)\|~^]+"), " "),
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"[^\w\s.,!?;:()\"'%-]", re.UNICODE), " "),
    (re.compile(r"\s+"), " "),
]

# Callers say "ru" the way they already do for ElevenLabs; v1 wants the full
# locale. Anything not listed is passed through as given, so "ru-RU" works too.
_LANGS = {
    "ru": "ru-RU",
    "kk": "kk-KK",
    "en": "en-US",
    "uz": "uz-UZ",
    "de": "de-DE",
    "he": "he-IL",
}

_TEXT_LIMIT = 5000  # v1 hard cap per request, in characters


class YandexSpeechKitAdapter(RetryClientMixin, ProviderAdapter):
    name = "yandex"
    env_var = "YANDEX_API_KEY"
    supported_tasks = {Task.TEXT_TO_SPEECH}
    SUPPORTED_PARAMS = {Task.TEXT_TO_SPEECH: ("voice", "language", "emotion", "speed")}

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        voice: str = "marina",
        folder_id: str | None = None,
        timeout_seconds: float = 60.0,
        retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._voice = voice
        self._folder_id = folder_id
        self._retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"Authorization": f"Api-Key {api_key}"} if api_key else {},
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        text = self._normalize(request.text or "")
        if not text:
            raise ProviderError("synthesize_speech requires non-empty `text`")
        if len(text) > _TEXT_LIMIT:
            raise ProviderError(
                f"SpeechKit v1 takes at most {_TEXT_LIMIT} characters per request, "
                f"got {len(text)}"
            )
        lang = str(request.params.get("language", "ru"))
        voice = str(request.params.get("voice") or self._voice)
        data: dict[str, Any] = {
            "text": text,
            "lang": _LANGS.get(lang.lower(), lang),
            "voice": voice,
            "format": "oggopus",
        }
        for knob in ("emotion", "speed"):
            value = request.params.get(knob)
            if value is not None:
                data[knob] = str(value)
        if self._folder_id:
            data["folderId"] = self._folder_id

        response = await self._request_with_retry(
            "POST", "/speech/v1/tts:synthesize", data=data
        )
        if not response.content:
            raise ProviderError("SpeechKit returned empty audio")

        return RouterResponse(
            task=Task.TEXT_TO_SPEECH,
            provider=self.name,
            model_id=model.model_id,
            # Characters, not seconds: SpeechKit bills the text it was given.
            usage=Usage(
                characters=len(text),
                raw={"characters": len(text), "voice": voice, "lang": data["lang"]},
            ),
            attachments=[Attachment(kind="audio", mime="audio/ogg", data=response.content)],
        )

    @staticmethod
    def _normalize(text: str) -> str:
        out = text.strip()
        for pattern, repl in _CLEAN_RE:
            out = pattern.sub(repl, out)
        return out.strip()
