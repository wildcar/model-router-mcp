"""The SpeechKit adapter against a fake transport. No network.

Two facts worth pinning: the exact form v1 receives (it is picky about field
names), and that a caller's two-letter language becomes the full locale —
"kk" must reach SpeechKit as "kk-KK", not fall back to Russian.
"""

from __future__ import annotations

import httpx
import pytest

from router_mcp.adapters.yandex import YandexSpeechKitAdapter
from router_mcp.domain.errors import ProviderError
from router_mcp.domain.messages import RouterRequest
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

BASE = "https://tts.api.cloud.yandex.net"

MODEL = RegistryModel(
    service_provider_id="yandex", model_provider="yandex",
    model_id="speechkit-tts-v1", display_name="Yandex SpeechKit TTS",
    tasks=(Task.TEXT_TO_SPEECH,), tier="expensive", source="seed",
)


def _adapter(handler, **kw) -> YandexSpeechKitAdapter:
    adapter = YandexSpeechKitAdapter(api_key="key-test", base_url=BASE, **kw)
    adapter._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(handler),
        headers={"Authorization": "Api-Key key-test"},
    )
    return adapter


def _request(text="Привет, мир!", **params) -> RouterRequest:
    return RouterRequest(
        task=Task.TEXT_TO_SPEECH, external_user_id="t", text=text, params=params
    )


async def test_form_fields_language_and_usage():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["form"] = dict(httpx.QueryParams(req.content.decode()))
        return httpx.Response(200, content=b"OggS-fake-opus")

    response = await _adapter(handler).generate(
        _request(language="kk", voice="amira"), MODEL
    )
    assert seen["path"] == "/speech/v1/tts:synthesize"
    assert seen["form"]["lang"] == "kk-KK"
    assert seen["form"]["voice"] == "amira"
    assert seen["form"]["format"] == "oggopus"
    # Optional knobs stay off the wire unless the caller sent them.
    assert "emotion" not in seen["form"] and "folderId" not in seen["form"]
    assert response.usage.characters == len("Привет, мир!")
    assert response.attachments[0].mime == "audio/ogg"
    assert response.model_id == "speechkit-tts-v1"


async def test_full_locale_passes_through_and_defaults_hold():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["form"] = dict(httpx.QueryParams(req.content.decode()))
        return httpx.Response(200, content=b"OggS")

    await _adapter(handler).generate(_request(language="ru-RU"), MODEL)
    assert seen["form"]["lang"] == "ru-RU"
    assert seen["form"]["voice"] == "marina"  # adapter default


async def test_provider_error_carries_the_body():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error_code": "BAD_REQUEST", "error_message": "unknown voice"}
        )

    with pytest.raises(ProviderError, match="unknown voice"):
        await _adapter(handler).generate(_request(voice="nope"), MODEL)


async def test_empty_text_is_rejected_before_the_wire():
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called")

    with pytest.raises(ProviderError, match="non-empty"):
        await _adapter(handler).generate(_request(text="   "), MODEL)
