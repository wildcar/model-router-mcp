"""ElevenLabs adapter — text-to-speech.

Ported from ort_bot/services/elevenlabs.py: resolve a voice id (by name if not
given), POST /v1/text-to-speech/{voice} for ogg/opus audio bytes. ElevenLabs
bills per character; we record the character count in usage.raw (the registry's
audio_price_per_second is the cost lever for v1, defaulting to 0).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from router_mcp.adapters.base import ProviderAdapter, RetryClientMixin
from router_mcp.domain.errors import ProviderError
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)

_CLEAN_RE = [
    (re.compile(r"[*_`#>\[\]\(\)\|~^]+"), " "),
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"[^\w\s.,!?;:()\"'%-]", re.UNICODE), " "),
    (re.compile(r"\s+"), " "),
]


class ElevenLabsAdapter(RetryClientMixin, ProviderAdapter):
    name = "elevenlabs"
    supported_modalities = {Modality.TTS}

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        model_id: str = "eleven_flash_v2_5",
        voice_name: str = "Nadia",
        voice_id: str | None = None,
        timeout_seconds: float = 60.0,
        retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._voice_name = voice_name
        self._voice_id = voice_id
        self._retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"xi-api-key": api_key or ""},
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        text = self._normalize(request.text or "")
        if not text:
            raise ProviderError("synthesize_speech requires non-empty `text`")
        voice_id = await self._resolve_voice_id(request.params.get("voice"))
        model_id = model.model_id or self._model_id

        response = await self._request_with_retry(
            "POST",
            f"/v1/text-to-speech/{voice_id}",
            params={"output_format": "opus_48000_64"},
            json={"text": text, "model_id": model_id, "language_code": request.params.get("language", "ru")},
            headers={"Accept": "audio/ogg"},
        )
        if not response.content:
            raise ProviderError("ElevenLabs returned empty audio")

        return RouterResponse(
            modality=Modality.TTS,
            provider=self.name,
            model_id=model_id,
            usage=Usage(raw={"characters": len(text), "voice_id": voice_id}),
            attachments=[Attachment(kind="audio", mime="audio/ogg", data=response.content)],
        )

    async def _resolve_voice_id(self, override: str | None) -> str:
        if override:
            return override
        if self._voice_id:
            return self._voice_id
        response = await self._request_with_retry(
            "GET", "/v2/voices", params={"search": self._voice_name, "page_size": 25}
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("ElevenLabs returned invalid JSON") from exc
        voices = payload.get("voices", []) if isinstance(payload, dict) else []
        exact = next(
            (
                v for v in voices
                if isinstance(v, dict)
                and str(v.get("name", "")).strip().casefold() == self._voice_name.casefold()
                and v.get("voice_id")
            ),
            None,
        )
        fallback = next((v for v in voices if isinstance(v, dict) and v.get("voice_id")), None)
        voice = exact or fallback
        if voice is None:
            raise ProviderError(f"No ElevenLabs voice named {self._voice_name!r}")
        self._voice_id = str(voice["voice_id"])
        logger.info("Resolved ElevenLabs voice %s -> %s", self._voice_name, self._voice_id)
        return self._voice_id

    @staticmethod
    def _normalize(text: str) -> str:
        out = text.strip()
        for pattern, repl in _CLEAN_RE:
            out = pattern.sub(repl, out)
        return out.strip()
