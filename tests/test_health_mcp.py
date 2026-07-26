"""The closed health summary, the key probe, and the MCP inventory.

Three things §12 and §16.5 ask for: the state of the installation behind the
auth gate rather than on an open endpoint, a live answer to "does this key still
work" (which `configured` cannot give), and the console finally having something
to say about MCP — the interface this service exists to provide.
"""

from __future__ import annotations

import pytest

from router_mcp.runtime import get_core
from router_mcp.web import health


def test_public_health_gives_nothing_away(client):
    """It is the one open route; a version and a provider list is a map."""
    assert client.get("/health").json() == {"status": "ok"}


def test_summary_carries_what_the_overview_shows(client):
    body = client.get("/api/v1/health/summary").json()
    assert body["version"]
    assert body["providers_total"] >= 1
    assert body["providers_usable"] <= body["providers_total"]
    assert body["uptime_seconds"] >= 0
    assert body["db_bytes"] > 0
    assert "last_24h" in body


def test_work_list_items_are_actionable(client):
    """Each line names something to fix and where to go and fix it."""
    work = client.get("/api/v1/health/summary").json()["work"]
    assert work, "an installation with unpriced seed rows always has work"
    for item in work:
        assert item["text"]
        assert item["href"].startswith("/")


def test_unpriced_seeds_show_up_as_work_not_as_free_models(client):
    """The accounting fix is visible from here: rows nobody priced are counted
    as a gap rather than passing for free models."""
    body = client.get("/api/v1/health/summary").json()
    assert body["models_unpriced"] > 0
    assert any("без цены" in item["text"] for item in body["work"])


@pytest.mark.asyncio
async def test_probe_reports_a_missing_key_by_variable_name(client):
    result = await health.probe_provider(get_core(), "openrouter", force=True)
    assert result.status == "not_configured"
    # The operator's next action is to set it, so it is named — never valued.
    assert "OPENROUTER_API_KEY" in result.detail


def test_probe_result_is_cached(client):
    """Probing on every page load would spend the rate limit on reassurance."""
    first = client.post("/api/v1/providers/openrouter/probe").json()
    second = client.post("/api/v1/providers/openrouter/probe").json()
    assert second["age_seconds"] >= first["age_seconds"]


def test_probe_of_an_unknown_provider_is_404(client):
    r = client.post("/api/v1/providers/nope/probe")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_mcp_summary_lists_every_tool_with_its_schema(client):
    body = client.get("/api/v1/mcp/summary").json()
    names = {tool["name"] for tool in body["tools"]}
    assert {"chat", "list_models", "run_cli", "get_usage"} <= names
    # Editing an image is its own tool, because it is its own capability: a model
    # that draws from a description usually cannot change a picture it is handed.
    assert {"generate_image", "edit_image"} <= names
    for tool in body["tools"]:
        # The schema is what makes the tab worth opening: it sits next to the
        # matching /api handle and any divergence shows up immediately.
        assert tool["input_schema"]["type"] == "object"
        assert tool["description"]
        assert tool["calls"] >= 0


def test_mcp_summary_says_it_is_stateless(client):
    """There will be no sessions screen — the server has no sessions to show."""
    body = client.get("/api/v1/mcp/summary").json()
    assert body["stateless"] is True


def test_mcp_probe_answers_in_process(client):
    body = client.post("/api/v1/mcp/probe").json()
    assert body["status"] == "ok"
    assert body["tools"] >= 7


def test_tool_and_client_stats_come_from_the_log(client, app_db_path):
    import sqlite3

    conn = sqlite3.connect(app_db_path)
    with conn:
        for i, (tool, status, latency) in enumerate(
            [("chat", "ok", 100), ("chat", "ok", 300), ("chat", "error", 200),
             ("run_cli", "ok", 62000), ("renamed_tool", "ok", 10)]
        ):
            conn.execute(
                "INSERT INTO request_logs (external_user_id, modality, channel, tool, "
                "client_id, status, latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("mcp-user", "chat", "mcp", tool, "telecodex" if i % 2 else None,
                 status, latency, "2026-07-26T10:00:00+00:00"),
            )
    conn.close()

    body = client.get("/api/v1/mcp/summary?hours=720").json()
    chat = next(t for t in body["tools"] if t["name"] == "chat")
    assert chat["calls"] == 3
    assert chat["errors"] == 1
    assert chat["p50_ms"] == 200
    # A tool the log knows and this build no longer exposes means an old client
    # is still asking for it.
    assert any(row["tool"] == "renamed_tool" for row in body["retired_tools_called"])
    clients = {c["client"] for c in body["clients"]}
    assert "telecodex" in clients and "(без метки)" in clients
