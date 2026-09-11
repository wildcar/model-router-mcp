"""Web app smoke test: lifespan boots, pages render, registry admin works.

Uses the session-scoped `client` from conftest: the FastMCP session manager can
only be run once per process, matching the real one-app-per-process deployment.
"""

from __future__ import annotations


def test_health_is_status_only(client):
    """Public health says alive and nothing else: the version and the provider
    inventory are a map of the installation, and this route is open."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_pages_render(client):
    for path in ("/", "/overview", "/models", "/logs", "/spend", "/images", "/connections"):
        r = client.get(path)
        assert r.status_code == 200
        assert "Model Router" in r.text


def test_navigation_has_seven_items(client):
    """«Матрица» is a mode of «Прогон» and MCP a tab of «Подключения» (§9);
    «Расход» and «Картинки» are screens of their own."""
    body = client.get("/").text
    for item in ("Обзор", "Прогон", "Модели", "Журнал", "Расход", "Картинки", "Подключения"):
        assert f">{item}</a>" in body
    assert ">Расход</a>" not in client.get("/logs").text.split("<main>")[1]


def test_old_spend_tab_link_lands_on_the_spend_screen(client):
    """«Расход» was `?tab=spend` on the journal; the bookmark must not open an
    empty journal."""
    r = client.get("/logs?tab=spend", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/spend"


def test_old_registry_link_still_resolves(client):
    """The screen split in two; a bookmark must not 404."""
    r = client.get("/registry", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/models"


def test_models_filtered_unknown_modality_empty(client):
    # An unknown modality matches nothing, regardless of keys.
    r = client.get("/api/models?modality=bogus")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_seed_models_present(client):
    # Fixed-catalog models are seeded regardless of whether keys are set.
    r = client.get("/api/models?modality=tts")
    assert r.status_code == 200
    tts_providers = {m["service_provider"] for m in r.json()["items"]}
    assert {"elevenlabs", "yandex"} <= tts_providers
    stt = client.get("/api/models?modality=stt").json()["items"]
    assert any(m["service_provider"] == "deepgram" for m in stt)
    cli = client.get("/api/models?modality=cli").json()["items"]
    assert {"codex-cli", "claude-cli"} <= {m["service_provider"] for m in cli}


def test_providers_listed(client):
    r = client.get("/api/registry/providers")
    assert r.status_code == 200
    body = r.json()["items"]
    ids = [p["id"] for p in body]
    assert "openrouter" in ids
    assert all(isinstance(p["configured"], bool) for p in body)
    # Every price in the registry must be traceable to a published source.
    priced = {"openrouter", "deepseek", "anthropic-api", "deepgram", "elevenlabs", "yandex"}
    assert all(p["pricing_url"] for p in body if p["id"] in priced)


def test_refresh_without_keys_reports_skips(client):
    r = client.post("/api/registry/refresh")
    assert r.status_code == 200, r.text
    reports = r.json()["providers"]
    # No API keys in this fixture, so nothing syncs — but every provider must
    # say why instead of failing silently.
    assert reports and all(rep["skipped"] for rep in reports)


def test_refresh_unknown_provider_is_a_client_error(client):
    r = client.post("/api/registry/refresh?provider=nope")
    assert r.status_code == 400
    # Every failure answers with the same envelope, keyed by a stable code.
    assert r.json()["error"]["code"] == "bad_request"


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
    rows = {m["model_id"]: m for m in r.json()["items"]}
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
    rows = client.get("/api/registry/models").json()["items"]
    assert [m for m in rows if m["service_provider"] == "deepseek"] == []
