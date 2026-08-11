"""The SpeechKit v3 adapter against a fake transport. No network.

The facts worth pinning: the hints the JSON body carries, the NDJSON chunk
reassembly, and the billing arithmetic — v3 charges each started block of 250
characters in full, so `usage.characters` must round up while the actual
length stays visible in raw.
"""

from __future__ import annotations

import base64
import json

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
    model_id="speechkit-tts-v3", display_name="Yandex SpeechKit TTS v3",
    tasks=(Task.TEXT_TO_SPEECH,), tier="cheap", source="seed",
)


def _chunk(data: bytes) -> str:
    return json.dumps(
        {"result": {"audioChunk": {"data": base64.b64encode(data).decode()}}}
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


async def test_hints_and_billable_characters():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.read())
        return httpx.Response(200, text=_chunk(b"OggS-fake"))

    response = await _adapter(handler).generate(
        _request(voice="saule", role="strict", speed=1.2), MODEL
    )
    assert seen["path"] == "/tts/v3/utteranceSynthesis"
    assert seen["body"]["hints"] == [
        {"voice": "saule"}, {"role": "strict"}, {"speed": 1.2}
    ]
    assert seen["body"]["outputAudioSpec"] == {
        "containerAudio": {"containerAudioType": "OGG_OPUS"}
    }
    # 12 actual characters bill as one full 250-character unit.
    assert "unsafeMode" not in seen["body"]
    assert response.usage.characters == 250
    assert response.usage.raw["characters"] == len("Привет, мир!")
    assert response.usage.raw["billing_units"] == 1
    assert response.attachments[0].mime == "audio/ogg"


async def test_long_text_turns_on_unsafe_mode_and_rounds_up():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.read())
        return httpx.Response(200, text=_chunk(b"OggS"))

    text = "ы" * 260  # два начатых блока
    response = await _adapter(handler).generate(_request(text=text), MODEL)
    assert seen["body"]["unsafeMode"] is True
    assert seen["body"]["hints"] == [{"voice": "marina"}]  # adapter default
    assert response.usage.characters == 500
    assert response.usage.raw["billing_units"] == 2


async def test_chunks_are_reassembled_in_order():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="\n".join([_chunk(b"Ogg"), _chunk(b"S-2")]))

    response = await _adapter(handler).generate(_request(), MODEL)
    assert response.attachments[0].data == b"OggS-2"


async def test_provider_error_carries_the_body():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"httpCode": 400,
                            "message": "role evil is not supported for voice marina"}},
        )

    with pytest.raises(ProviderError, match="not supported for voice"):
        await _adapter(handler).generate(_request(role="evil"), MODEL)


async def test_mid_stream_error_is_surfaced():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=_chunk(b"Ogg") + "\n" + json.dumps({"error": {"message": "boom"}})
        )

    with pytest.raises(ProviderError, match="mid-stream"):
        await _adapter(handler).generate(_request(), MODEL)


async def test_empty_text_is_rejected_before_the_wire():
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called")

    with pytest.raises(ProviderError, match="non-empty"):
        await _adapter(handler).generate(_request(text="   "), MODEL)


async def test_output_format_picks_the_container_and_the_mime():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.read())
        return httpx.Response(200, text=_chunk(b"mp3-bytes"))

    # The ElevenLabs-style string clients already send lands on MP3 by prefix.
    response = await _adapter(handler).generate(
        _request(output_format="mp3_44100_128"), MODEL
    )
    assert seen["body"]["outputAudioSpec"] == {
        "containerAudio": {"containerAudioType": "MP3"}
    }
    assert response.attachments[0].mime == "audio/mpeg"
    assert response.usage.raw["container"] == "MP3"


async def test_unknown_format_is_refused_before_the_wire():
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called")

    with pytest.raises(ProviderError, match="supported output_format"):
        await _adapter(handler).generate(_request(output_format="flac"), MODEL)
