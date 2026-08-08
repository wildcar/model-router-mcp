"""The normalized `web_search` flag on the OpenRouter adapter. No network.

The contract under test: the caller says a boolean, the adapter speaks
OpenRouter's dialect — server tool first, the legacy `web` plugin when the tool
fails transiently, no search as the last resort. Two things must never happen:
a payload without the flag growing search fields, and the ladder swallowing an
error that is not the search's fault (a bad key, a rejected prompt).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from router_mcp.adapters.base import ignored_params
from router_mcp.adapters.openrouter import OpenRouterAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.errors import AccessDenied, ProviderError, ProviderUnavailable
from router_mcp.domain.messages import RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database

BASE = "https://openrouter.test/api/v1"


def _ok_body(text: str = "нашлось") -> dict[str, Any]:
    return {
        "id": "gen-1",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _script(responses: list[httpx.Response]):
    """A handler that answers from the script and keeps every request body."""
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return responses[min(len(bodies), len(responses)) - 1]

    return handle, bodies


def _adapter(handler) -> OpenRouterAdapter:
    # retries=0 so each rung of the search ladder is exactly one HTTP call.
    adapter = OpenRouterAdapter(api_key="sk-test", base_url=BASE, retries=0)
    adapter._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(handler)
    )
    return adapter


def _model(model_id: str = "openai/gpt-4o") -> RegistryModel:
    return RegistryModel(
        service_provider_id="openrouter",
        model_provider="openai",
        model_id=model_id,
        display_name=model_id,
        modalities=(Modality.CHAT,),
        tier="cheap",
        prompt_price=1e-7,
        completion_price=1e-7,
        source="manual",
        price_source_kind="manual",
    )


def _request(**params: Any) -> RouterRequest:
    return RouterRequest(
        task=Task.TEXT_TO_TEXT, external_user_id="u", text="что нового?", params=params
    )


# --- payload shape ----------------------------------------------------------

@pytest.mark.asyncio
async def test_without_web_search_the_payload_is_unchanged():
    handler, bodies = _script([httpx.Response(200, json=_ok_body())])
    response = await _adapter(handler).generate(_request(temperature=0.5), _model())
    assert bodies[0]["temperature"] == 0.5
    assert "tools" not in bodies[0]
    assert "plugins" not in bodies[0]
    assert "web_search" not in bodies[0]
    assert "web_search_mode" not in response.raw_meta


@pytest.mark.asyncio
async def test_web_search_asks_the_server_tool_first():
    handler, bodies = _script([httpx.Response(200, json=_ok_body())])
    response = await _adapter(handler).generate(_request(web_search=True), _model())
    assert bodies[0]["tools"] == [{"type": "openrouter:web_search"}]
    assert "plugins" not in bodies[0]
    # The flag itself is normalized here, never forwarded as a payload field.
    assert "web_search" not in bodies[0]
    assert response.raw_meta["web_search"] is True
    assert response.raw_meta["web_search_mode"] == "server_tool"


# --- the ladder -------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_transient_tool_failure_falls_back_to_the_legacy_plugin():
    # A 200 whose body is a transient top-level error counts the same as a 5xx:
    # both are how OpenRouter says "not right now".
    handler, bodies = _script([
        httpx.Response(200, json={"error": {"message": "search backend down", "code": 503}}),
        httpx.Response(200, json=_ok_body()),
    ])
    response = await _adapter(handler).generate(_request(web_search=True), _model())
    assert bodies[1]["plugins"] == [{"id": "web"}]
    assert "tools" not in bodies[1]
    assert response.raw_meta["web_search_mode"] == "plugin"


@pytest.mark.asyncio
async def test_when_the_plugin_also_fails_the_request_runs_without_search():
    handler, bodies = _script([
        httpx.Response(500, text="boom"),
        httpx.Response(502, text="boom"),
        httpx.Response(200, json=_ok_body()),
    ])
    response = await _adapter(handler).generate(_request(web_search=True), _model())
    assert len(bodies) == 3
    assert "tools" not in bodies[2] and "plugins" not in bodies[2]
    # The degradation is stated, not hidden: the caller asked for search and
    # must learn from the reply that none happened.
    assert response.raw_meta["web_search"] is True
    assert response.raw_meta["web_search_mode"] == "off"


@pytest.mark.asyncio
async def test_a_dead_provider_still_fails_after_the_last_rung():
    handler, bodies = _script([httpx.Response(500, text="boom")])
    with pytest.raises(ProviderUnavailable):
        await _adapter(handler).generate(_request(web_search=True), _model())
    assert len(bodies) == 3  # all three rungs tried, then the truth comes out


# --- errors the ladder must not mask ----------------------------------------

@pytest.mark.asyncio
async def test_an_authorization_failure_is_not_masked_by_the_ladder():
    handler, bodies = _script([httpx.Response(401, text="bad key")])
    with pytest.raises(AccessDenied):
        await _adapter(handler).generate(_request(web_search=True), _model())
    # One call: retrying a bad key without search would spend a request to
    # discover the same 401 and report it as a search problem.
    assert len(bodies) == 1


@pytest.mark.asyncio
async def test_a_rejected_prompt_is_not_masked_by_the_ladder():
    handler, bodies = _script([
        httpx.Response(200, json={"error": {"message": "Prompt rejected", "code": 400}}),
    ])
    with pytest.raises(ProviderError, match="Prompt rejected"):
        await _adapter(handler).generate(_request(web_search=True), _model())
    assert len(bodies) == 1


# --- declaration ------------------------------------------------------------

def test_web_search_is_declared_for_text_tasks_only():
    adapter = OpenRouterAdapter(api_key=None, base_url=BASE)
    assert ignored_params(adapter, Task.TEXT_TO_TEXT, {"web_search": True}) == []
    assert ignored_params(adapter, Task.IMAGE_TO_TEXT, {"web_search": True}) == []
    # On image generation the flag is named as ignored, not silently dropped.
    assert ignored_params(adapter, Task.TEXT_TO_IMAGE, {"web_search": True}) == ["web_search"]


@pytest.mark.asyncio
async def test_on_an_undeclared_task_the_flag_stays_out_of_the_payload():
    handler, bodies = _script([httpx.Response(200, json=_ok_body())])
    request = RouterRequest(
        task=Task.TEXT_TO_IMAGE, external_user_id="u", text="нарисуй кота",
        params={"web_search": True},
    )
    await _adapter(handler).generate(request, _model())
    assert len(bodies) == 1
    assert "tools" not in bodies[0] and "plugins" not in bodies[0]


# --- the journal -------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_journal_keeps_the_flag_and_the_mode_that_actually_ran(tmp_path):
    handler, _bodies = _script([
        httpx.Response(500, text="search down"),
        httpx.Response(200, json=_ok_body()),
    ])
    adapter = _adapter(handler)
    db = Database(str(tmp_path / "ws.db"))
    await db.init()
    await db.upsert_service_provider("openrouter", "OpenRouter", "openrouter", None, enabled=True)
    await db.upsert_registry_model(_model())
    router = ModelRouter(
        db, RegistryService(db, adapters={}), {"openrouter": adapter},
        BlobStore(str(tmp_path / "blobs")),
    )
    try:
        result = await router.dispatch(
            RouterRequest(
                task=Task.TEXT_TO_TEXT, external_user_id="u",
                model_id="openai/gpt-4o", text="что нового?",
                params={"web_search": True},
            )
        )
        assert result.response.raw_meta["web_search_mode"] == "plugin"

        row = await db.get_request_log(result.log_id)
        assert row is not None
        # The request side: the flag as the caller sent it.
        assert json.loads(row["request_json"])["params"]["web_search"] is True
        # The execution side: the timeline names the mode that answered.
        timeline = json.loads(row["plan_json"])
        titles = [s.get("title", "") for s in timeline]
        assert any("пробую плагин web" in t for t in titles), titles
        assert any(t == "веб-поиск: плагин web" for t in titles), titles
    finally:
        await db.close()
