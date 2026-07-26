"""Spend: four kinds of money that must not be added together.

Real money, money the provider quoted but never charged, and requests whose cost
nobody can state are different facts. One total hides all three, and a total
that hides them is worse than no total — it is a confident wrong answer (§7).
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture(scope="module", autouse=True)
def spend_rows(client, app_db_path):
    """One row per price state, with distinct amounts so a mix-up is visible."""
    conn = sqlite3.connect(app_db_path)
    rows = [
        # user,      cost,  source,              tokens, day
        ("acc-bot", 8.10, "computed", 1000, "2026-07-20"),
        ("acc-bot", 3.90, "estimated", 500, "2026-07-21"),
        ("acc-bot", 0.84, "provider_notional", 200, "2026-07-22"),
        ("acc-web", 0.00, "unpriced", 300, "2026-07-22"),
        ("acc-web", 0.00, "cost_unknown", 0, "2026-07-23"),
    ]
    with conn:
        for user, cost, source, tokens, day in rows:
            conn.execute(
                """
                INSERT INTO request_logs (
                    external_user_id, modality, service_provider_id, model_id,
                    status, channel, total_tokens, prompt_tokens, cost_usd,
                    cost_source, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (user, "chat", "openrouter", "acc/model", "ok", "api",
                 tokens, tokens, cost, source, f"{day}T10:00:00+00:00"),
            )
    conn.close()


def _spend(client, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    body = client.get(f"/api/v1/usage?{query}&user=acc-bot").json()
    return body["facets"]["totals"], body["items"]


def test_the_kinds_of_money_stay_apart(client):
    totals, _items = _spend(client, group_by="user")
    assert totals["confirmed_usd"] == pytest.approx(8.10)
    assert totals["unconfirmed_usd"] == pytest.approx(3.90)
    # Notional money is reported and excluded from the total.
    assert totals["notional_usd"] == pytest.approx(0.84)
    assert totals["total_usd"] == pytest.approx(12.00)


def test_unpriced_requests_make_the_total_a_lower_bound(client):
    body = client.get("/api/v1/usage?group_by=user&user=acc-web").json()
    totals = body["facets"]["totals"]
    assert totals["unpriced_requests"] == 1
    assert totals["unknown_requests"] == 1
    # The screen must be able to say "не менее" rather than a bare figure.
    assert totals["is_lower_bound"] is True


def test_a_clean_selection_is_not_a_lower_bound(client):
    totals, _items = _spend(client, group_by="user")
    assert totals["is_lower_bound"] is False


@pytest.mark.parametrize("group_by", ["user", "model", "provider", "channel", "day"])
def test_every_grouping_answers(client, group_by):
    body = client.get(f"/api/v1/usage?group_by={group_by}").json()
    assert body["items"]
    assert body["facets"]["group_by"] == group_by
    assert all("bucket" in item for item in body["items"])


def test_unknown_grouping_is_a_client_error(client):
    r = client.get("/api/v1/usage?group_by=; DROP TABLE request_logs")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"
    # And the table is still there.
    assert client.get("/api/v1/logs?limit=1").status_code == 200


def test_confirmed_share_flags_a_mostly_guessed_bill(client):
    _totals, items = _spend(client, group_by="user")
    row = next(i for i in items if i["bucket"] == "acc-bot")
    # 8.10 confirmed out of 12.00 — the console warns below one half.
    assert row["confirmed_share"] == pytest.approx(8.10 / 12.00)


def test_daily_series_is_ordered_for_the_sparkline(client):
    facets = client.get("/api/v1/usage?group_by=user").json()["facets"]
    days = [d["day"] for d in facets["daily"]]
    assert days == sorted(days)


def test_export_keeps_the_price_sources_in_separate_columns(client):
    r = client.get("/api/v1/usage/export?group_by=user&user=acc-bot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    header, *lines = r.text.strip().splitlines()
    # Collapsing these into one column in the export would launder exactly the
    # distinction the screen works to keep.
    assert "confirmed_usd" in header and "unconfirmed_usd" in header
    assert "notional_usd" in header and "unpriced_requests" in header
    assert lines
