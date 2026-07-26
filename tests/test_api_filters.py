"""Log API: projection, filters, cursor paging, and the error contract on the wire.

Offset paging was the bug waiting to happen here — the log grows from the head,
so by the time the operator asks for page two, page one has shifted and rows are
shown twice or skipped. Everything below pins the behaviour the console relies
on (§13, §17.1 of docs/UI_CONCEPT_v2.md).
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture(scope="module", autouse=True)
def seeded(client, app_db_path):
    """Sixty rows across two users, three channels and both statuses.

    Written with plain sqlite3 against the same file: the app's async connection
    belongs to the TestClient's event loop, and borrowing it from the test thread
    would be a race, not a shortcut.
    """
    conn = sqlite3.connect(app_db_path)
    with conn:
        for i in range(60):
            conn.execute(
                """
                INSERT INTO request_logs (
                    external_user_id, modality, service_provider_id, model_id,
                    status, error_message, channel, total_tokens, cost_usd,
                    cost_source, attempts, requested_model_id, response_text,
                    created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "bot-77" if i % 2 else "web-tester",
                    "chat",
                    "openrouter",
                    "openai/gpt-4o-mini",
                    "error" if i % 10 == 0 else "ok",
                    "NotConfigured: no key" if i % 10 == 0 else None,
                    ("mcp", "ui", "api")[i % 3],
                    100 + i,
                    0.0001,
                    "estimated" if i % 5 == 0 else "computed",
                    3 if i % 7 == 0 else 1,
                    "openai/gpt-4o" if i % 11 == 0 else None,
                    "a" * 100,
                    f"2026-07-26T10:{i:02d}:00+00:00",
                ),
            )
    conn.close()


def test_list_answers_with_an_envelope(client):
    body = client.get("/api/v1/logs?limit=50").json()
    assert set(body) == {"items", "total", "next_cursor", "facets"}
    assert body["total"] == 60
    assert len(body["items"]) == 50
    assert body["next_cursor"] == body["items"][-1]["id"]


def test_list_is_projected_not_the_whole_row(client):
    """Fifty full rows carry fifty response bodies and fifty raw usage blocks to
    draw a table of numbers."""
    item = client.get("/api/v1/logs?limit=1").json()["items"][0]
    assert "response_text" not in item
    assert "usage_raw" not in item
    # ...but everything the table shows is there.
    assert {"id", "created_at", "external_user_id", "model_id", "channel",
            "status", "cost_source", "latency_ms", "attempts"} <= set(item)


def test_cursor_pages_without_repeating(client):
    first = client.get("/api/v1/logs?limit=25").json()
    second = client.get(f"/api/v1/logs?limit=25&cursor={first['next_cursor']}").json()
    ids_first = [r["id"] for r in first["items"]]
    ids_second = [r["id"] for r in second["items"]]
    assert not set(ids_first) & set(ids_second)
    assert ids_first == sorted(ids_first, reverse=True)
    assert max(ids_second) < min(ids_first)


@pytest.mark.parametrize(
    "params, predicate",
    [
        ("user=bot-77", lambda r: r["external_user_id"] == "bot-77"),
        ("status=error", lambda r: r["status"] == "error"),
        ("channel=mcp", lambda r: r["channel"] == "mcp"),
        ("with_fallback=true", lambda r: r["requested_model_id"] is not None),
        ("with_retries=true", lambda r: r["attempts"] > 1),
        ("unverified_cost=true", lambda r: r["cost_source"] == "estimated"),
    ],
)
def test_filters(client, params, predicate):
    body = client.get(f"/api/v1/logs?limit=200&{params}").json()
    assert body["items"], params
    assert all(predicate(r) for r in body["items"])
    assert body["total"] == len(body["items"])


def test_detail_returns_the_run_envelope(client):
    log_id = client.get("/api/v1/logs?limit=1&status=error").json()["items"][0]["id"]
    run = client.get(f"/api/v1/logs/{log_id}").json()
    assert run["log_id"] == log_id
    assert run["status"] == "error"
    # The failure text is in the database; before this it never reached a screen.
    assert run["error"]["message"] == "NotConfigured: no key"
    assert run["error"]["log_id"] == log_id


def test_missing_entry_is_a_typed_404(client):
    r = client.get("/api/v1/logs/999999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_legacy_prefix_still_answers(client):
    """A page loaded before a restart keeps working while it is open."""
    assert client.get("/api/logs?limit=1").status_code == 200
