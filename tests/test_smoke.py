"""End-to-end vertical-slice test with a stub adapter (no network).

Proves the chain: registry row -> router resolve -> adapter.generate -> cost
computation -> request_logs write -> usage aggregation.
"""

from __future__ import annotations

import pytest

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database


class StubChatAdapter(ProviderAdapter):
    name = "stub"
    supported_modalities = {Modality.CHAT}

    def is_configured(self) -> bool:
        return True

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        return RouterResponse(
            modality=Modality.CHAT,
            provider=self.name,
            model_id=model.model_id,
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15, raw={"x": 1}),
            text=f"echo: {request.text}",
        )


async def _make_core(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.upsert_service_provider("stub", "Stub", "stub", None, enabled=True)
    await db.upsert_registry_model(
        RegistryModel(
            service_provider_id="stub",
            model_provider="stub",
            model_id="stub/chat-1",
            display_name="Stub Chat",
            modalities=(Modality.CHAT,),
            tier="cheap",
            prompt_price=1e-6,
            completion_price=2e-6,
            source="manual",
        )
    )
    registry = RegistryService(db, openrouter_api_key=None, openrouter_base_url="http://x")
    blobs = BlobStore(str(tmp_path / "blobs"))
    router = ModelRouter(db, registry, {"stub": StubChatAdapter()}, blobs)
    return db, registry, router


@pytest.mark.asyncio
async def test_dispatch_logs_and_costs(tmp_path):
    db, registry, router = await _make_core(tmp_path)
    try:
        req = RouterRequest(
            modality=Modality.CHAT, external_user_id="u1", model_id="stub/chat-1", text="hi"
        )
        result = await router.dispatch(req)

        assert result.response.text == "echo: hi"
        # cost = 10*1e-6 + 5*2e-6 = 2e-5
        assert result.cost.cost_usd == pytest.approx(2e-5)
        assert result.cost.cost_source == "computed"

        logs = await db.list_request_logs(external_user_id="u1")
        assert len(logs) == 1
        assert logs[0]["status"] == "ok"
        assert logs[0]["total_tokens"] == 15
        assert logs[0]["external_user_id"] == "u1"

        usage = await db.get_usage(external_user_id="u1")
        assert len(usage) == 1
        assert usage[0].requests == 1
        assert usage[0].total_tokens == 15
        assert usage[0].total_cost_usd == pytest.approx(2e-5)
    finally:
        await registry.close()
        await db.close()


@pytest.mark.asyncio
async def test_resolve_by_tier(tmp_path):
    db, registry, router = await _make_core(tmp_path)
    try:
        req = RouterRequest(modality=Modality.CHAT, external_user_id="u2", tier="cheap", text="yo")
        result = await router.dispatch(req)
        assert result.response.model_id == "stub/chat-1"
    finally:
        await registry.close()
        await db.close()


@pytest.mark.asyncio
async def test_unknown_model_errors_and_logs(tmp_path):
    from router_mcp.domain.errors import RouterError

    db, registry, router = await _make_core(tmp_path)
    try:
        req = RouterRequest(
            modality=Modality.CHAT, external_user_id="u3", model_id="nope/x", text="hi"
        )
        with pytest.raises(RouterError):
            await router.dispatch(req)
        logs = await db.list_request_logs(external_user_id="u3")
        assert len(logs) == 1
        assert logs[0]["status"] == "error"
    finally:
        await registry.close()
        await db.close()
