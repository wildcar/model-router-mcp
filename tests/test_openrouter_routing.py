"""Pinning an OpenRouter model to chosen hosts. No network.

OpenRouter serves z-ai/glm-5.3-flash from some thirty hosts priced $0.14-$1.00
per 1M out, and its catalog shows only the cheapest, so the journal ran 15%
under the charge on average and 3x under on bad days (prod, 2026-09-27). The
row's `provider_routing` goes out as the `provider` object, and the host
OpenRouter picked lands in the run timeline.
"""

from __future__ import annotations

import json

import httpx
import pytest

from router_mcp.adapters.openrouter import OpenRouterAdapter
from router_mcp.domain import progress
from router_mcp.domain.messages import RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

BASE = "https://openrouter.test/api/v1"


def _adapter():
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={
            "id": "gen-1",
            "provider": "Novita",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 1e-6},
        })

    adapter = OpenRouterAdapter(api_key="sk-test", base_url=BASE, retries=0)
    adapter._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handle))
    return adapter, calls


def _model(**kw) -> RegistryModel:
    return RegistryModel(
        service_provider_id="openrouter", model_provider="z-ai",
        model_id="z-ai/glm-5.3-flash", display_name="GLM 5.3 Flash",
        modalities=(Modality.CHAT,), price_source_kind="manual", **kw,
    )


@pytest.mark.asyncio
async def test_pinned_hosts_go_out_as_the_provider_object():
    adapter, calls = _adapter()
    routing = {"only": ["gmicloud/fp8", "novita/fp8"], "allow_fallbacks": False}
    await adapter.generate(RouterRequest(Task.TEXT_TO_TEXT, "u", text="hi"),
                           _model(provider_routing=routing))
    assert json.loads(calls[0].content.decode())["provider"] == routing


@pytest.mark.asyncio
async def test_an_unpinned_row_leaves_the_choice_to_openrouter():
    adapter, calls = _adapter()
    await adapter.generate(RouterRequest(Task.TEXT_TO_TEXT, "u", text="hi"), _model())
    assert "provider" not in json.loads(calls[0].content.decode())


@pytest.mark.asyncio
async def test_the_serving_host_lands_in_the_timeline():
    adapter, _ = _adapter()
    events: list[dict] = []
    token = progress.set_sink(events.append)
    try:
        await adapter.generate(RouterRequest(Task.TEXT_TO_TEXT, "u", text="hi"), _model())
    finally:
        progress.reset_sink(token)
    assert {"kind": "upstream", "title": "хост: Novita", "host": "Novita"} in events
