"""The ElevenLabs adapter against a fake transport. No network.

What is pinned here: a caller's `voice` may be either form — a voice_id goes
into the URL as-is (no search round-trip), a human name is resolved through
/v2/voices once and then served from the cache — and the engine the registry
row names is the engine the request body carries.
"""

from __future__ import annotations

import httpx
import pytest

from router_mcp.adapters.elevenlabs import ElevenLabsAdapter
from router_mcp.domain.errors import ProviderError
from router_mcp.domain.messages import RouterRequest
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

BASE = "https://api.elevenlabs.io"
NADIA_ID = "gedzfqL7OGdPbwm0ynTP"


def _model(model_id="eleven_multilingual_v2") -> RegistryModel:
    return RegistryModel(
        service_provider_id="elevenlabs", model_provider="elevenlabs",
        model_id=model_id, display_name=model_id,
        tasks=(Task.TEXT_TO_SPEECH,), tier="expensive", source="seed",
    )


def _request(**params) -> RouterRequest:
    return RouterRequest(
        task=Task.TEXT_TO_SPEECH, external_user_id="t", text="Проверка.", params=params
    )


def _adapter(handler) -> ElevenLabsAdapter:
    adapter = ElevenLabsAdapter(api_key="xi-test", base_url=BASE)
    adapter._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(handler),
        headers={"xi-api-key": "xi-test"},
    )
    return adapter


def _handler(seen):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/voices":
            seen["searches"] = seen.get("searches", 0) + 1
            seen["search_for"] = req.url.params.get("search")
            return httpx.Response(
                200,
                json={"voices": [
                    {"name": "Nadia - Energetic, Clear and Polished", "voice_id": NADIA_ID}
                ]},
            )
        seen["tts_path"] = req.url.path
        seen["body"] = req.read()
        return httpx.Response(200, content=b"OggS-fake")

    return handler


async def test_a_voice_id_is_used_as_is_without_a_search():
    seen: dict = {}
    await _adapter(_handler(seen)).generate(_request(voice=NADIA_ID), _model())
    assert seen.get("searches", 0) == 0
    assert seen["tts_path"] == f"/v1/text-to-speech/{NADIA_ID}"


async def test_a_voice_name_is_searched_once_then_cached():
    seen: dict = {}
    adapter = _adapter(_handler(seen))
    await adapter.generate(_request(voice="Nadia"), _model())
    await adapter.generate(_request(voice="Nadia"), _model())
    assert seen["searches"] == 1
    assert seen["search_for"] == "Nadia"
    assert seen["tts_path"] == f"/v1/text-to-speech/{NADIA_ID}"


async def test_the_registry_rows_engine_reaches_the_request_body():
    seen: dict = {}
    await _adapter(_handler(seen)).generate(
        _request(voice=NADIA_ID), _model("eleven_flash_v2_5")
    )
    assert b'"model_id": "eleven_flash_v2_5"' in seen["body"] or \
        b'"model_id":"eleven_flash_v2_5"' in seen["body"]


async def test_an_unknown_voice_name_is_a_provider_error():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v2/voices"
        return httpx.Response(200, json={"voices": []})

    with pytest.raises(ProviderError, match="No ElevenLabs voice"):
        await _adapter(handler).generate(_request(voice="Nobody"), _model())
