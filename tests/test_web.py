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
        OPENROUTER_API_KEY=None,
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


def test_models_filtered_modality_empty(client):
    # No TTS models registered and no API key, so this is reliably empty.
    r = client.get("/api/models?modality=tts")
    assert r.status_code == 200
    assert r.json() == []


def test_providers_listed(client):
    r = client.get("/api/registry/providers")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()]
    assert "openrouter" in ids


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
    ids = [m["model_id"] for m in r.json()]
    assert "demo/manual-1" in ids
