"""Yandex SpeechKit adapter — text-to-speech, TTS API v3.

`POST /tts/v3/utteranceSynthesis` (REST): a JSON request that answers with a
stream of newline-delimited JSON chunks, each carrying a base64 audio piece;
the adapter reassembles them into one OGG/Opus blob. Auth is a service-account
Api-Key (`YANDEX_API_KEY`).

v3 has no language field: the voice implies the language (`amira` is Kazakh,
`marina` is Russian — see docs/speechkit-voices.md), and roles replace v1's
`emotion`. Billing is per request unit of 250 characters (started units count
in full), so `usage.characters` reports the *billable* count rounded up to a
whole unit — the actual text length is in `usage.raw["characters"]` — and the
registry's `character_price` is the unit price divided by 250.
"""

from __future__ import annotations

import base64
import json
import logging
import math
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

_TEXT_LIMIT = 5000  # sane cap; v3 itself takes longer texts in unsafe mode
_BILLING_BLOCK = 250  # v3 bills each started block of 250 characters in full


class YandexSpeechKitAdapter(RetryClientMixin, ProviderAdapter):
    name = "yandex"
    env_var = "YANDEX_API_KEY"
    supported_tasks = {Task.TEXT_TO_SPEECH}
    SUPPORTED_PARAMS = {Task.TEXT_TO_SPEECH: ("voice", "role", "speed")}

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
        headers = {"Authorization": f"Api-Key {api_key}"} if api_key else {}
        if folder_id:
            headers["x-folder-id"] = folder_id
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_seconds, headers=headers
        )

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        text = self._normalize(request.text or "")
        if not text:
            raise ProviderError("synthesize_speech requires non-empty `text`")
        if len(text) > _TEXT_LIMIT:
            raise ProviderError(
                f"SpeechKit adapter takes at most {_TEXT_LIMIT} characters per "
                f"request, got {len(text)}"
            )
        voice = str(request.params.get("voice") or self._voice)
        hints: list[dict[str, Any]] = [{"voice": voice}]
        role = request.params.get("role")
        if role is not None:
            hints.append({"role": str(role)})
        speed = request.params.get("speed")
        if speed is not None:
            hints.append({"speed": float(speed)})

        body: dict[str, Any] = {
            "text": text,
            "hints": hints,
            "outputAudioSpec": {"containerAudio": {"containerAudioType": "OGG_OPUS"}},
        }
        if len(text) >= _BILLING_BLOCK:
            # v3 caps a plain request at one billing block; longer texts are
            # legal only with this flag (billed per started block either way).
            body["unsafeMode"] = True

        response = await self._request_with_retry(
            "POST", "/tts/v3/utteranceSynthesis", json=body
        )
        audio = self._assemble(response)
        if not audio:
            raise ProviderError("SpeechKit returned empty audio")

        units = max(1, math.ceil(len(text) / _BILLING_BLOCK))
        return RouterResponse(
            task=Task.TEXT_TO_SPEECH,
            provider=self.name,
            model_id=model.model_id,
            # Billable characters, not actual ones: v3 charges each started
            # 250-character unit in full, and the log must count money the way
            # the provider does. The actual length is in raw.
            usage=Usage(
                characters=units * _BILLING_BLOCK,
                raw={
                    "characters": len(text),
                    "billing_units": units,
                    "voice": voice,
                    "role": role,
                },
            ),
            attachments=[Attachment(kind="audio", mime="audio/ogg", data=audio)],
        )

    @staticmethod
    def _assemble(response: httpx.Response) -> bytes:
        """Glue the NDJSON chunk stream back into one audio blob."""
        audio = b""
        for line in response.text.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise ProviderError("SpeechKit returned an invalid chunk") from exc
            if "error" in payload:
                raise ProviderError(f"SpeechKit error mid-stream: {payload['error']}")
            data = payload.get("result", {}).get("audioChunk", {}).get("data")
            if data:
                audio += base64.b64decode(data)
        return audio

    @staticmethod
    def _normalize(text: str) -> str:
        out = text.strip()
        for pattern, repl in _CLEAN_RE:
            out = pattern.sub(repl, out)
        return out.strip()
