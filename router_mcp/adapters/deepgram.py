"""Deepgram adapter — speech-to-text.

Ported from ort_bot/services/deepgram.py: POST /listen with the audio bytes,
parse the transcript, fall back to nova-2 on INSUFFICIENT_PERMISSIONS. Audio
duration (for metering) comes from the response metadata.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from router_mcp.adapters.base import ProviderAdapter, RetryClientMixin
from router_mcp.domain.errors import ProviderError
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)


class DeepgramAdapter(RetryClientMixin, ProviderAdapter):
    name = "deepgram"
    supported_modalities = {Modality.STT}

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        model: str = "nova-2",
        timeout_seconds: float = 60.0,
        retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"Authorization": f"Token {api_key or ''}"},
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        audio = next((a.data for a in request.attachments if a.data), None)
        if audio is None:
            raise ProviderError("transcribe requires an audio attachment with bytes")
        mime = next((a.mime for a in request.attachments if a.data), "audio/ogg")
        requested_model = model.model_id or self._model

        payload = await self._listen(audio, mime, requested_model)
        if (
            isinstance(payload, dict)
            and payload.get("err_code") == "INSUFFICIENT_PERMISSIONS"
            and requested_model.lower() != "nova-2"
        ):
            payload = await self._listen(audio, mime, "nova-2")

        transcript = self._extract_transcript(payload)
        duration = self._extract_duration(payload)
        return RouterResponse(
            modality=Modality.STT,
            provider=self.name,
            model_id=requested_model,
            usage=Usage(audio_seconds=duration, raw=payload.get("metadata", {}) if isinstance(payload, dict) else {}),
            text=transcript,
        )

    async def _listen(self, audio: bytes, mime: str, model: str) -> dict[str, Any]:
        response = await self._request_with_retry(
            "POST",
            "/listen",
            params={
                "model": model,
                "smart_format": "true",
                "detect_language": "true",
                "punctuate": "true",
            },
            content=audio,
            headers={"Content-Type": mime or "audio/ogg"},
        )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("Deepgram returned invalid JSON") from exc

    @staticmethod
    def _extract_transcript(payload: Any) -> str:
        channels = payload.get("results", {}).get("channels", []) if isinstance(payload, dict) else []
        if not channels:
            raise ProviderError("Deepgram returned no channels")
        alternatives = channels[0].get("alternatives", [])
        if not alternatives:
            raise ProviderError("Deepgram returned no transcript")
        transcript = alternatives[0].get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            raise ProviderError("Could not transcribe the audio")
        return transcript.strip()

    @staticmethod
    def _extract_duration(payload: Any) -> float | None:
        if isinstance(payload, dict):
            meta = payload.get("metadata")
            if isinstance(meta, dict):
                dur = meta.get("duration")
                if isinstance(dur, (int, float)):
                    return float(dur)
        return None
