"""Application identity: who is asking, said to providers and kept in the log.

The contract (SPEC §36): `app_url` (the site the app runs on) wins, `app_name`
is the fallback, both are untrusted grouping keys. OpenRouter — the one provider
with such a parameter — receives them as HTTP-Referer / X-Title per request; a
request that carries neither goes out as the router itself. The journal stores
the effective identity, filters by substring and suggests seen values.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest

from router_mcp.adapters.openrouter import OpenRouterAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.adapters.base import ProviderAdapter
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database
from router_mcp.web.views import run_from_dispatch, run_from_log

BASE = "https://openrouter.test/api/v1"


def _ok_body() -> dict[str, Any]:
    return {
        "id": "gen-1",
        "choices": [{"message": {"content": "привет"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def _adapter():
    """An adapter over MockTransport that keeps every request's headers."""
    seen: list[httpx.Headers] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json=_ok_body())

    adapter = OpenRouterAdapter(api_key="sk-test", base_url=BASE, retries=0)
    adapter._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(handle)
    )
    return adapter, seen


def _model() -> RegistryModel:
    return RegistryModel(
        service_provider_id="openrouter",
        model_provider="openai",
        model_id="openai/gpt-4o",
        display_name="gpt-4o",
        modalities=(Modality.CHAT,),
        source="manual",
        price_source_kind="manual",
    )


def _request(**kwargs: Any) -> RouterRequest:
    return RouterRequest(Task.TEXT_TO_TEXT, "u", text="ping", **kwargs)


@pytest.mark.asyncio
async def test_openrouter_forwards_the_app_identity():
    adapter, seen = _adapter()
    await adapter.generate(
        _request(app_url="https://agentish.example", app_name="Agentish"), _model()
    )
    assert seen[0]["HTTP-Referer"] == "https://agentish.example"
    assert seen[0]["X-Title"] == "Agentish"


@pytest.mark.asyncio
async def test_url_alone_is_not_titled_with_our_name():
    """An app identified by its site must not appear under the router's title."""
    adapter, seen = _adapter()
    await adapter.generate(_request(app_url="https://agentish.example"), _model())
    assert seen[0]["HTTP-Referer"] == "https://agentish.example"
    assert "x-title" not in seen[0]


@pytest.mark.asyncio
async def test_name_alone_rides_under_the_router_site():
    adapter, seen = _adapter()
    await adapter.generate(_request(app_name="Agentish"), _model())
    assert seen[0]["HTTP-Referer"] == "https://mr.wildcar.org"
    assert seen[0]["X-Title"] == "Agentish"


@pytest.mark.asyncio
async def test_no_identity_means_the_router_itself():
    adapter, seen = _adapter()
    await adapter.generate(_request(), _model())
    assert seen[0]["HTTP-Referer"] == "https://mr.wildcar.org"
    assert seen[0]["X-Title"] == "model-router-mcp"


@pytest.mark.asyncio
async def test_cyrillic_name_is_transliterated_readably():
    """Header values are ASCII, and OpenRouter prints X-Title verbatim: a
    percent-encoded «Проверка» reached its dashboard as %D0%9F… (seen live).
    The title must stay letters a human can read."""
    adapter, seen = _adapter()
    await adapter.generate(
        _request(app_name="Проверка идентификации"), _model()
    )
    assert seen[0]["X-Title"] == "Proverka identifikatsii"


@pytest.mark.asyncio
async def test_kazakh_letters_are_transliterated_too():
    adapter, seen = _adapter()
    await adapter.generate(_request(app_name="Қазақ Үні"), _model())
    assert seen[0]["X-Title"] == "Qazaq Uni"


@pytest.mark.asyncio
async def test_other_scripts_fall_back_to_percent_encoding():
    """No table covers every alphabet; what it cannot romanize must still
    survive the wire instead of crashing httpx."""
    adapter, seen = _adapter()
    await adapter.generate(_request(app_name="你好"), _model())
    assert seen[0]["X-Title"] == "%E4%BD%A0%E5%A5%BD"


@pytest.mark.asyncio
async def test_cyrillic_referer_stays_percent_encoded():
    """The referer is a URL, and percent-encoding is how URLs carry non-ASCII —
    a transliterated URL would name a host that does not exist."""
    adapter, seen = _adapter()
    await adapter.generate(_request(app_url="https://сайт.рф/х"), _model())
    # The ASCII structure of the URL survives; only the Cyrillic is escaped.
    assert seen[0]["HTTP-Referer"] == (
        "https://%D1%81%D0%B0%D0%B9%D1%82.%D1%80%D1%84/%D1%85"
    )


# --- the journal ---------------------------------------------------------
class StubAdapter(ProviderAdapter):
    name = "stub"
    supported_tasks = {Task.TEXT_TO_TEXT}
    SUPPORTED_PARAMS = {Task.TEXT_TO_TEXT: ()}

    def is_configured(self) -> bool:
        return True

    async def generate(self, request, model):
        return RouterResponse(
            Task.TEXT_TO_TEXT, "stub", model.model_id,
            Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2), text="hi",
        )


async def _dispatch(tmp_path, **request_kwargs):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.upsert_service_provider("stub", "Stub", "stub", None, enabled=True)
    await db.upsert_registry_model(
        RegistryModel("stub", "stub", "stub/model", "Stub", (Modality.CHAT,))
    )
    router = ModelRouter(
        db, RegistryService(db, adapters={}), {"stub": StubAdapter()},
        BlobStore(str(tmp_path / "b")),
    )
    request = RouterRequest(
        Task.TEXT_TO_TEXT, "u", model_id="stub/model", text="ping", **request_kwargs
    )
    result = await router.dispatch(request)
    return db, request, result


@pytest.mark.asyncio
async def test_the_site_wins_and_the_log_keeps_both_halves(tmp_path):
    db, request, result = await _dispatch(
        tmp_path, app_url="https://agentish.example", app_name="Agentish"
    )
    try:
        row = await db.get_request_log(result.log_id)
        assert row["app"] == "https://agentish.example"
        snap = json.loads(row["request_json"])
        assert snap["app_url"] == "https://agentish.example"
        assert snap["app_name"] == "Agentish"
        for run in (run_from_dispatch(result, request), run_from_log(row)):
            assert run["app"] == "https://agentish.example"
            assert run["request"]["app_name"] == "Agentish"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_name_is_the_fallback_identity(tmp_path):
    db, _request, result = await _dispatch(tmp_path, app_name="Agentish")
    try:
        row = await db.get_request_log(result.log_id)
        assert row["app"] == "Agentish"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_identity_is_an_honest_null(tmp_path):
    db, _request, result = await _dispatch(tmp_path)
    try:
        row = await db.get_request_log(result.log_id)
        assert row["app"] is None
    finally:
        await db.close()


# --- the web filter ------------------------------------------------------
@pytest.fixture(scope="module")
def app_rows(client, app_db_path):
    """Three rows under distinctive app identities, straight into the file.

    The assertions below always filter by these markers, so the module leaves
    the shared session log usable for everyone else.
    """
    conn = sqlite3.connect(app_db_path)
    with conn:
        for app in ("https://appident-a.example", "https://appident-b.example", None):
            conn.execute(
                "INSERT INTO request_logs (external_user_id, modality, status, app, "
                "created_at) VALUES (?,?,?,?,?)",
                ("appident-user", "chat", "ok", app, "2026-08-26T10:00:00+00:00"),
            )
    conn.close()


def test_logs_filter_by_app_substring(client, app_rows):
    body = client.get("/api/v1/logs", params={"app": "appident-a"}).json()
    assert body["total"] == 1
    assert body["items"][0]["app"] == "https://appident-a.example"
    # Case-insensitive substring, like the user box.
    both = client.get("/api/v1/logs", params={"app": "APPIDENT"}).json()
    assert both["total"] == 2


def test_log_list_rows_carry_the_app_column(client, app_rows):
    items = client.get(
        "/api/v1/logs", params={"user": "appident-user"}
    ).json()["items"]
    assert {r["app"] for r in items} == {
        "https://appident-a.example", "https://appident-b.example", None,
    }


def test_suggest_offers_seen_apps(client, app_rows):
    body = client.get(
        "/api/v1/logs/suggest", params={"field": "app", "q": "appident"}
    ).json()
    assert set(body["items"]) == {
        "https://appident-a.example", "https://appident-b.example",
    }


def test_usage_groups_by_app(client, app_rows):
    body = client.get("/api/v1/usage", params={"group_by": "app"}).json()
    buckets = {r["bucket"] for r in body["items"]}
    assert {"https://appident-a.example", "https://appident-b.example"} <= buckets
