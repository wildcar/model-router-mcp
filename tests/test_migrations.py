"""A deployed database is patched by one restart, and the patch must be safe.

`request_logs` is the only record of what was ever spent. The task columns of
ADR-0006 are additive for exactly that reason, and this file is the proof: the
old columns come out of the migration byte for byte, and a row written before the
change prices through `compute_cost` the same as it did.

The old schema is spelled out here on purpose. Reusing `Database.init()` to build
the "before" state would test the migration against itself.
"""

from __future__ import annotations

import aiosqlite
import pytest

from router_mcp.accounting.cost import compute_cost
from router_mcp.domain.messages import Usage
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.storage.database import Database

# The shape a deployed database has just before ADR-0006: no `tasks`, no
# `compute_price_per_second`, no `task`, no `compute_seconds`.
_OLD_SCHEMA = """
CREATE TABLE service_providers (
    id TEXT PRIMARY KEY, display_name TEXT NOT NULL, adapter TEXT NOT NULL,
    base_url TEXT, enabled INTEGER NOT NULL DEFAULT 1, pricing_url TEXT,
    created_at DATETIME NOT NULL
);
CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, service_provider_id TEXT NOT NULL,
    name TEXT NOT NULL, monthly_cost REAL NOT NULL DEFAULT 0,
    monthly_quota_units REAL, period_start TEXT, created_at DATETIME NOT NULL
);
CREATE TABLE registry_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_provider_id TEXT NOT NULL, model_provider TEXT NOT NULL,
    model_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
    modality TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'cheap',
    prompt_price REAL NOT NULL DEFAULT 0, completion_price REAL NOT NULL DEFAULT 0,
    image_price REAL NOT NULL DEFAULT 0, audio_price_per_second REAL NOT NULL DEFAULT 0,
    character_price REAL NOT NULL DEFAULT 0, cache_read_price REAL NOT NULL DEFAULT 0,
    cache_write_price REAL NOT NULL DEFAULT 0, context_length INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual', is_active INTEGER NOT NULL DEFAULT 1,
    pricing_mode TEXT NOT NULL DEFAULT 'per_request', subscription_id INTEGER,
    last_seen_at DATETIME, deprecated_at DATETIME, replaced_by TEXT,
    price_source TEXT, price_source_kind TEXT NOT NULL DEFAULT 'estimate',
    price_checked_at DATETIME, revision INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    UNIQUE(service_provider_id, model_id)
);
CREATE TABLE request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, external_user_id TEXT NOT NULL,
    service_provider_id TEXT, model_provider TEXT, model_id TEXT,
    modality TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ok', error_message TEXT,
    request_text TEXT, request_ref TEXT, request_json TEXT,
    response_text TEXT, response_ref TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
    audio_seconds REAL, images INTEGER, characters INTEGER, usage_raw TEXT,
    cost_usd REAL NOT NULL DEFAULT 0, cost_source TEXT NOT NULL DEFAULT 'computed',
    pricing_mode TEXT NOT NULL DEFAULT 'per_request', latency_ms INTEGER,
    finish_reason TEXT, requested_model_id TEXT, fallback_reason TEXT,
    channel TEXT NOT NULL DEFAULT 'api', tool TEXT, client_id TEXT,
    client_request_id TEXT, batch_id TEXT, attempts INTEGER, provider_status INTEGER,
    provider_request_id TEXT, session_id TEXT, return_code INTEGER,
    plan_json TEXT, finished_at DATETIME, created_at DATETIME NOT NULL
);
"""

# One row per legacy modality, plus the vision-model shape that carries two.
_OLD_MODELS = [
    ("openrouter", "openai", "gpt-4o", "chat,image", 1e-6, 2e-6),
    ("openrouter", "meta", "llama", "chat", 5e-7, 5e-7),
    ("codex-oauth", "openai", "gpt-image-2", "image", 0.0, 0.0),
    ("deepgram", "deepgram", "nova-2", "stt", 0.0, 0.0),
    ("elevenlabs", "elevenlabs", "flash", "tts", 0.0, 0.0),
    ("claude-cli", "anthropic", "claude", "cli", 0.0, 0.0),
]

_OLD_LOGS = [
    ("u1", "chat", 1000, 500, 0.0015),
    ("u1", "image", None, None, 0.04),
    ("u2", "stt", None, None, 0.002),
    ("u2", "tts", None, None, 0.01),
    ("u3", "cli", 2000, 300, 0.05),
]


async def _build_old(path: str) -> None:
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(_OLD_SCHEMA)
        await conn.execute(
            "INSERT INTO service_providers (id, display_name, adapter, base_url, "
            "enabled, created_at) VALUES ('openrouter', 'OpenRouter', 'openrouter', "
            "NULL, 1, '2026-01-01T00:00:00Z')"
        )
        for sp, vendor, model_id, modality, prompt, completion in _OLD_MODELS:
            await conn.execute(
                "INSERT INTO registry_models (service_provider_id, model_provider, "
                "model_id, display_name, modality, tier, prompt_price, "
                "completion_price, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'cheap', ?, ?, 'catalog', ?, ?)",
                (sp, vendor, model_id, model_id, modality, prompt, completion,
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        # Two rows that name their model: the confirmation backfill reads these,
        # and it must count the answer and not the failure.
        for model_id, status, when in (
            ("llama", "ok", "2026-02-01T00:00:00Z"),
            ("llama", "ok", "2026-03-01T00:00:00Z"),
            ("gpt-4o", "error", "2026-04-01T00:00:00Z"),
        ):
            await conn.execute(
                "INSERT INTO request_logs (external_user_id, service_provider_id, "
                "model_id, modality, status, cost_usd, cost_source, created_at) "
                "VALUES ('u4', 'openrouter', ?, 'chat', ?, 0, 'computed', ?)",
                (model_id, status, when),
            )
        for user, modality, prompt, completion, cost in _OLD_LOGS:
            await conn.execute(
                "INSERT INTO request_logs (external_user_id, modality, prompt_tokens, "
                "completion_tokens, cost_usd, cost_source, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'computed', '2026-01-01T00:00:00Z')",
                (user, modality, prompt, completion, cost),
            )
        await conn.commit()


@pytest.fixture
async def migrated(tmp_path):
    """An old database taken through `init()` once, as a restart would."""
    path = str(tmp_path / "old.db")
    await _build_old(path)
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM request_logs ORDER BY id") as cur:
            before = [dict(r) for r in await cur.fetchall()]
    db = Database(path)
    await db.init()
    yield db, before
    await db.close()


async def test_the_new_columns_arrive(migrated):
    db, _ = migrated
    for table, column in (
        ("registry_models", "tasks"),
        ("registry_models", "compute_price_per_second"),
        ("request_logs", "task"),
        ("request_logs", "compute_seconds"),
    ):
        rows = await db._fetchall(f"PRAGMA table_info({table})")
        assert column in {r["name"] for r in rows}, f"{table}.{column} missing"


async def test_the_money_columns_come_out_untouched(migrated):
    """`modality`, the token counts and the sums are the record of spend. If the
    migration can change them, it can change history."""
    db, before = migrated
    after_rows = await db._fetchall("SELECT * FROM request_logs ORDER BY id")
    after = [dict(r) for r in after_rows]
    assert len(after) == len(before)
    for old, new in zip(before, after):
        for column, value in old.items():
            assert new[column] == value, f"{column} changed on row {old['id']}"


@pytest.mark.parametrize(
    "modality,task",
    [
        ("chat", "text-to-text"),
        ("image", "text-to-image"),
        ("stt", "speech-to-text"),
        ("tts", "text-to-speech"),
        ("cli", "cli"),
    ],
)
async def test_log_rows_get_their_task(migrated, modality: str, task: str):
    db, _ = migrated
    rows = await db._fetchall(
        "SELECT task FROM request_logs WHERE modality = ?", (modality,)
    )
    assert rows and all(r["task"] == task for r in rows)


async def test_registry_rows_get_their_tasks(migrated):
    db, _ = migrated
    rows = await db._fetchall("SELECT model_id, modality, tasks FROM registry_models")
    got = {r["model_id"]: r["tasks"] for r in rows}
    # A row carrying two groups gets a task per group, in the same order.
    assert got["gpt-4o"] == "text-to-text,text-to-image"
    assert got["llama"] == "text-to-text"
    assert got["gpt-image-2"] == "text-to-image"
    assert got["nova-2"] == "speech-to-text"
    assert got["flash"] == "text-to-speech"
    assert got["claude"] == "cli"


async def test_a_migrated_registry_row_loads_and_still_prices(migrated):
    """The end-to-end point: an old row comes back as a usable model with both
    axes filled, and costs the same as before."""
    db, _ = migrated
    models = {m.model_id: m for m in await db.list_registry_models()}
    llama = models["llama"]
    assert llama.tasks == (Task.TEXT_TO_TEXT,)
    assert llama.modalities == (Modality.CHAT,)
    assert llama.compute_price_per_second == 0.0
    assert llama.supports(Task.TEXT_TO_TEXT)
    assert not llama.supports(Task.IMAGE_TO_IMAGE)

    result = compute_cost(
        llama, Modality.CHAT, Usage(prompt_tokens=1000, completion_tokens=500)
    )
    assert result.cost_usd == pytest.approx(1000 * 5e-7 + 500 * 5e-7)


async def test_the_migration_is_idempotent(migrated):
    """A second restart must not append a second task to every row."""
    db, _ = migrated
    await db._add_missing_columns()
    rows = await db._fetchall("SELECT tasks FROM registry_models WHERE model_id = 'llama'")
    assert rows[0]["tasks"] == "text-to-text"


async def test_history_counts_as_confirmation(migrated):
    """A model in daily use must not come out of the migration looking unproven:
    the first sweep of unconfirmed rows would retire it. The date is the last
    successful request, and a failed attempt confirms nothing."""
    db, _ = migrated
    rows = await db._fetchall("SELECT model_id, confirmed_at FROM registry_models")
    got = {r["model_id"]: r["confirmed_at"] for r in rows}
    assert got["llama"] == "2026-03-01T00:00:00Z"
    assert got["gpt-4o"] is None       # only an error row names it
    assert got["nova-2"] is None       # never addressed
