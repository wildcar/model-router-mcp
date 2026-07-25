"""Web app smoke test: lifespan boots, pages render, registry admin works.

The FastMCP session manager can only be run once per process, so the app (and
thus the TestClient lifespan) is module-scoped — matching the real one-app-per-
process deployment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from router_mcp.app import build_app
from router_mcp.config import Settings


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("web")
    settings = Settings(
        DB_PATH=str(tmp_path / "web.db"),
        BLOB_DIR=str(tmp_path / "blobs"),
        LOG_FILE=str(tmp_path / "log.txt"),
        # Every provider key is cleared explicitly: Settings still reads the
        # repo .env, and a configured adapter would make the catalog sync call
        # the real API during tests.
        OPENROUTER_API_KEY=None,
        DEEPSEEK_API_KEY=None,
        ANTHROPIC_API_KEY=None,
        DEEPGRAM_API_KEY=None,
        ELEVENLABS_API_KEY=None,
        CATALOG_SYNC_INTERVAL_SECONDS=0,
    )
    app = build_app(settings)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "openrouter" in body["providers"]


def test_pages_render(client):
    for path in ("/", "/registry", "/logs"):
        r = client.get(path)
        assert r.status_code == 200
        assert "Model Router" in r.text


def test_models_filtered_unknown_modality_empty(client):
    # An unknown modality matches nothing, regardless of keys.
    r = client.get("/api/models?modality=bogus")
    assert r.status_code == 200
    assert r.json() == []


def test_seed_models_present(client):
    # Fixed-catalog models are seeded regardless of whether keys are set.
    r = client.get("/api/models?modality=tts")
    assert r.status_code == 200
    assert any(m["service_provider"] == "elevenlabs" for m in r.json())
    assert any(m["service_provider"] == "deepgram" for m in client.get("/api/models?modality=stt").json())
    cli = client.get("/api/models?modality=cli").json()
    assert {"codex-cli", "claude-cli"} <= {m["service_provider"] for m in cli}


def test_providers_listed(client):
    r = client.get("/api/registry/providers")
    assert r.status_code == 200
    body = r.json()
    ids = [p["id"] for p in body]
    assert "openrouter" in ids
    assert all(isinstance(p["configured"], bool) for p in body)
    # Every price in the registry must be traceable to a published source.
    priced = {"openrouter", "deepseek", "anthropic-api", "deepgram", "elevenlabs"}
    assert all(p["pricing_url"] for p in body if p["id"] in priced)


def test_refresh_without_keys_reports_skips(client):
    r = client.post("/api/registry/refresh")
    assert r.status_code == 200, r.text
    reports = r.json()["providers"]
    # No API keys in this fixture, so nothing syncs — but every provider must
    # say why instead of failing silently.
    assert reports and all(rep["skipped"] for rep in reports)


def test_refresh_unknown_provider_is_a_client_error(client):
    assert client.post("/api/registry/refresh?provider=nope").status_code == 400


def test_add_manual_model_and_list(client):
    body = {
        "service_provider_id": "openrouter",
        "model_id": "demo/manual-1",
        "display_name": "Manual demo",
        "modalities": ["chat"],
        "prompt_price": 0.000001,
        "completion_price": 0.000002,
    }
    r = client.post("/api/registry/models", json=body)
    assert r.status_code == 200, r.text
    # 3e-6 USD/token => $0.003 per 1K tokens, above the $0.001 cheap threshold.
    assert r.json()["tier"] == "expensive"

    r = client.get("/api/registry/models?source=manual")
    rows = {m["model_id"]: m for m in r.json()}
    assert "demo/manual-1" in rows
    added = rows["demo/manual-1"]
    # A hand-entered price counts as verified and falls back to the provider's
    # price page as its source.
    assert added["price_source_kind"] == "manual"
    assert added["price_is_verified"] is True
    assert added["price_source"] == "https://openrouter.ai/models"
    assert added["price_checked_at"]


def test_deepseek_has_no_hardcoded_models(client):
    """The retired deepseek-chat/deepseek-reasoner ids used to come back on every
    restart. Without keys DeepSeek should now contribute nothing at all."""
    rows = client.get("/api/registry/models").json()
    assert [m for m in rows if m["service_provider"] == "deepseek"] == []
