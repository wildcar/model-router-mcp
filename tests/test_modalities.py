"""Modality dispatch tests with stub adapters (no network): image, STT, TTS.

Verifies per-modality cost branches and blob persistence of binary outputs.
"""

from __future__ import annotations

import pytest

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database


class StubAdapter(ProviderAdapter):
    def __init__(self, name, modality, response):
        self.name = name
        self.supported_tasks = {modality}
        self._response = response

    def is_configured(self) -> bool:
        return True

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        return self._response


async def _core(tmp_path, provider_id, model: RegistryModel, response):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.upsert_service_provider(provider_id, provider_id, provider_id, None, enabled=True)
    await db.upsert_registry_model(model)
    registry = RegistryService(db, adapters={})
    blobs = BlobStore(str(tmp_path / "blobs"))
    adapter = StubAdapter(provider_id, model.modalities[0], response)
    router = ModelRouter(db, registry, {provider_id: adapter}, blobs)
    return db, registry, router, blobs


@pytest.mark.asyncio
async def test_image_cost_and_blob(tmp_path):
    model = RegistryModel("img", "img", "img/gen", "Img", (Modality.IMAGE,), image_price=0.04)
    resp = RouterResponse(
        Task.TEXT_TO_IMAGE, "img", "img/gen", Usage(images=1),
        attachments=[Attachment(kind="image", mime="image/png", data=b"PNGBYTES")],
    )
    db, registry, router, blobs = await _core(tmp_path, "img", model, resp)
    try:
        result = await router.dispatch(
            RouterRequest(Task.TEXT_TO_IMAGE, "u", model_id="img/gen", text="a cat")
        )
        assert result.cost.cost_usd == pytest.approx(0.04)
        ref = result.response.attachments[0].ref
        assert ref and blobs.exists(ref)
        assert blobs.load(ref) == b"PNGBYTES"
        logs = await db.list_request_logs(external_user_id="u")
        assert logs[0]["images"] == 1
        # response_ref is detail, not a table column: the list projection only
        # carries what the log table on screen shows.
        detail = await db.get_request_log(logs[0]["id"])
        assert detail["response_ref"] == ref
    finally:
        await registry.close(); await db.close()


@pytest.mark.asyncio
async def test_stt_cost(tmp_path):
    model = RegistryModel("dg", "dg", "dg/nova", "STT", (Modality.STT,), audio_price_per_second=0.001)
    resp = RouterResponse(Task.SPEECH_TO_TEXT, "dg", "dg/nova", Usage(audio_seconds=10.0), text="hello")
    db, registry, router, blobs = await _core(tmp_path, "dg", model, resp)
    try:
        req = RouterRequest(
            Task.SPEECH_TO_TEXT, "u", model_id="dg/nova",
            attachments=[Attachment(kind="audio", mime="audio/ogg", data=b"AUDIO")],
        )
        result = await router.dispatch(req)
        assert result.response.text == "hello"
        assert result.cost.cost_usd == pytest.approx(0.01)  # 10s * 0.001
        # request audio persisted to blob
        logs = await db.list_request_logs(external_user_id="u")
        assert logs[0]["audio_seconds"] == 10.0
        detail = await db.get_request_log(logs[0]["id"])
        assert detail["request_ref"]
    finally:
        await registry.close(); await db.close()


@pytest.mark.asyncio
async def test_tts_cost_and_audio_blob(tmp_path):
    model = RegistryModel("el", "el", "el/v2", "TTS", (Modality.TTS,), audio_price_per_second=0.001)
    resp = RouterResponse(
        Task.TEXT_TO_SPEECH, "el", "el/v2", Usage(audio_seconds=5.0),
        attachments=[Attachment(kind="audio", mime="audio/ogg", data=b"OGGBYTES")],
    )
    db, registry, router, blobs = await _core(tmp_path, "el", model, resp)
    try:
        result = await router.dispatch(
            RouterRequest(Task.TEXT_TO_SPEECH, "u", model_id="el/v2", text="привет")
        )
        assert result.cost.cost_usd == pytest.approx(0.005)
        ref = result.response.attachments[0].ref
        assert ref and blobs.load(ref) == b"OGGBYTES"
    finally:
        await registry.close(); await db.close()
