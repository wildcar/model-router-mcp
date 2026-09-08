"""SQLite persistence (aiosqlite, raw SQL).

Connection handling follows the ort_bot/database.py pattern: a single shared
async connection guarded by `_conn_lock`, with all writes serialized through
`_write_lock`. Schema is created idempotently in `init()`.

Tables:
  service_providers  — connection adapters (openrouter, deepseek, ...)
  registry_models    — catalog + manual model rows (3-level mapping + pricing)
  subscriptions      — monthly pools for amortization (hook; not billed in v1)
  request_logs       — per-request/response log keyed by external_user_id
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import PriceSchedule, RegistryModel


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class ServiceProviderRecord:
    id: str
    display_name: str
    adapter: str
    enabled: bool
    base_url: str | None
    created_at: str
    pricing_url: str | None = None


@dataclass(slots=True)
class SubscriptionRecord:
    id: int
    service_provider_id: str
    name: str
    monthly_cost: float
    monthly_quota_units: float | None
    period_start: str | None
    created_at: str


@dataclass(slots=True)
class RequestLogRecord:
    external_user_id: str
    modality: str  # the billing group; what the money was counted in
    task: str | None = None  # what was asked for (ADR-0006); null on older rows
    service_provider_id: str | None = None
    model_provider: str | None = None
    model_id: str | None = None
    status: str = "ok"
    error_message: str | None = None
    request_text: str | None = None
    request_ref: str | None = None
    # The request as the router received it — model/tier hints, params, the
    # fallback policy, the message list. `request_text` alone answers "что
    # спросили" and nothing about how it was asked, which is half of what an
    # operator opens an entry for. Never carries credentials: this is the
    # router's own request, not the body sent upstream (§16.4).
    request_json: dict[str, Any] | None = None
    response_text: str | None = None
    response_ref: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    audio_seconds: float | None = None
    # Seconds of provider compute, the billable figure the provider itself
    # reported. Not our wall clock, and not the length of what came back.
    compute_seconds: float | None = None
    images: int | None = None
    characters: int | None = None
    usage_raw: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    # "computed" | "estimated" | "manual" | "amortized" | "provider"
    # | "provider_notional" | "unpriced" | "cost_unknown"
    cost_source: str = "computed"
    pricing_mode: str = "per_request"
    latency_ms: int | None = None
    finish_reason: str | None = None  # provider stop reason, verbatim
    requested_model_id: str | None = None  # set when a fallback substituted a model
    fallback_reason: str | None = None
    # Call provenance: who called, through which interface, with which tool.
    channel: str = "api"  # "mcp" | "api" | "ui"
    tool: str | None = None
    client_id: str | None = None
    client_request_id: str | None = None
    batch_id: str | None = None  # one operator action that fanned out into N runs
    # The effective application identity (site, else name). One column, not
    # two: the journal filters by "which app", and the split lives in the
    # request snapshot for the rows that need it.
    app: str | None = None
    # Execution detail the console's timeline and error card need.
    attempts: int | None = None  # HTTP tries inside the successful provider attempt
    provider_status: int | None = None  # last upstream HTTP status
    provider_request_id: str | None = None  # upstream id, for support tickets
    # The sum the provider itself said it charged (OpenRouter's `usage.cost`).
    # Its own column, not a key inside `usage_raw`: SQL compares it with
    # `cost_usd`, and the difference is how a stale or incomplete registry
    # price gets noticed (§7.4) — twice already, in fact.
    provider_cost_usd: float | None = None
    session_id: str | None = None  # CLI engines
    return_code: int | None = None  # CLI engines
    plan_json: str | None = None  # routing plan, for replaying the timeline
    finished_at: str | None = None


@dataclass(slots=True)
class UsageRow:
    external_user_id: str
    requests: int
    total_tokens: int
    total_cost_usd: float


def _col(row: aiosqlite.Row, name: str, default: Any = None) -> Any:
    """A column that may be absent — a projected SELECT, or a row read before
    `_add_missing_columns` had run. `aiosqlite.Row` raises on a missing key."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _token_clause(column: str) -> str:
    """Match one whole token of a comma-joined column.

    Four alternatives instead of `LIKE '%x%'` because a substring match would let
    `text-to-text` answer a query for `image-to-text`.
    """
    return (
        f"({column} = ? OR {column} LIKE ? OR {column} LIKE ? OR {column} LIKE ?)"
    )


def _token_params(token: str) -> list[str]:
    return [token, f"{token},%", f"%,{token}", f"%,{token},%"]


def _model_to_row(m: RegistryModel) -> dict[str, Any]:
    return {
        "service_provider_id": m.service_provider_id,
        "model_provider": m.model_provider,
        "model_id": m.model_id,
        "display_name": m.display_name,
        # Two columns, two questions: `tasks` is what the model can be asked to
        # do and is what routing filters on; `modality` is the group it bills in
        # and is what the spend reports group by (ADR-0006).
        "tasks": ",".join(task.value for task in m.tasks),
        "modality": ",".join(mod.value for mod in m.modalities),
        "capabilities": ",".join(m.capabilities),
        "tier": m.tier,
        "prompt_price": m.prompt_price,
        "completion_price": m.completion_price,
        "image_price": m.image_price,
        "audio_price_per_second": m.audio_price_per_second,
        "character_price": m.character_price,
        "cache_read_price": m.cache_read_price,
        "cache_write_price": m.cache_write_price,
        "web_search_price": m.web_search_price,
        "compute_price_per_second": m.compute_price_per_second,
        "context_length": m.context_length,
        "source": m.source,
        "is_active": 1 if m.is_active else 0,
        "pricing_mode": m.pricing_mode,
        "subscription_id": m.subscription_id,
        "last_seen_at": m.last_seen_at,
        "deprecated_at": m.deprecated_at,
        "replaced_by": m.replaced_by,
        "retired_by": m.retired_by,
        "price_source": m.price_source,
        "price_source_kind": m.price_source_kind,
        "price_checked_at": m.price_checked_at,
        # `confirmed_at` is deliberately absent: it is a fact about our own
        # traffic, and a catalog sync writing this row must not touch it. It is
        # set by `confirm_registry_model` alone.
        #
        # `input_map`, `extra_params` and `price_schedule` are absent for the same
        # reason, one step further: only a human knows that `garm_img` is the
        # garment, that this upscaler's `scale` is worth exposing, or that the
        # rate doubles between 01:00 and 04:00 UTC — no catalog publishes any of
        # it, so a sync writing the empty defaults here would erase it on every
        # refresh. All three are set by `update_registry_model` alone.
    }


def _row_to_model(row: aiosqlite.Row) -> RegistryModel:
    modalities = tuple(
        Modality.from_str(part)
        for part in str(row["modality"]).split(",")
        if part.strip()
    )
    # A row written before the column existed reads as empty here, and
    # `RegistryModel.__post_init__` then derives the tasks from the groups. Which
    # is why the migration's backfill and this fallback say the same thing.
    tasks = tuple(
        Task.from_str(part)
        for part in str(_col(row, "tasks") or "").split(",")
        if part.strip()
    )
    caps = tuple(part for part in str(row["capabilities"]).split(",") if part.strip())
    return RegistryModel(
        service_provider_id=row["service_provider_id"],
        model_provider=row["model_provider"],
        model_id=row["model_id"],
        display_name=row["display_name"],
        modalities=modalities,
        tasks=tasks,
        capabilities=caps,
        tier=row["tier"],
        prompt_price=row["prompt_price"],
        completion_price=row["completion_price"],
        image_price=row["image_price"],
        audio_price_per_second=row["audio_price_per_second"],
        character_price=row["character_price"],
        cache_read_price=row["cache_read_price"],
        cache_write_price=row["cache_write_price"],
        web_search_price=_col(row, "web_search_price") or 0.0,
        compute_price_per_second=_col(row, "compute_price_per_second") or 0.0,
        context_length=row["context_length"],
        source=row["source"],
        is_active=bool(row["is_active"]),
        pricing_mode=row["pricing_mode"],
        subscription_id=row["subscription_id"],
        last_seen_at=row["last_seen_at"],
        deprecated_at=row["deprecated_at"],
        replaced_by=row["replaced_by"],
        retired_by=_col(row, "retired_by"),
        price_source=row["price_source"],
        price_source_kind=row["price_source_kind"] or "estimate",
        price_checked_at=row["price_checked_at"],
        updated_at=row["updated_at"],
        revision=row["revision"],
        confirmed_at=_col(row, "confirmed_at"),
        input_map=_json_map(_col(row, "input_map")),
        extra_params=tuple(
            part.strip()
            for part in str(_col(row, "extra_params") or "").split(",")
            if part.strip()
        ),
        price_schedule=_price_schedule(_col(row, "price_schedule")),
        description=str(_col(row, "description") or ""),
    )


def _price_schedule(raw: Any) -> PriceSchedule | None:
    """The stored schedule, or none at all for a column nobody can read.

    Same bargain `_json_map` makes: the API validates what an operator types, and
    a row that got past it broken loads at its base rate rather than taking the
    whole registry down. Billing round the clock at the off-peak figure is wrong
    by a factor of two for a few hours; a registry that will not load is wrong
    for everything.
    """
    try:
        return PriceSchedule.parse(raw)
    except ValueError:
        return None


def _json_map(raw: Any) -> dict[str, str]:
    """A JSON object column as `{str: str}`, or empty for anything else.

    Hand-edited text in a TEXT column is the one thing here that can be malformed
    at rest, and a registry that refuses to load because one row has a stray brace
    takes every model down with it. Empty means "bind positionally", which is a
    working answer rather than an outage.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if k and v}


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._conn_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        async with self._conn_lock:
            if self._conn is not None:
                return self._conn
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            return self._conn

    async def close(self) -> None:
        async with self._conn_lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    async def init(self) -> None:
        conn = await self._get_conn()
        async with self._write_lock:
            await conn.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS service_providers (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    base_url TEXT,
                    pricing_url TEXT,
                    created_at DATETIME NOT NULL
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_provider_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    monthly_cost REAL NOT NULL DEFAULT 0,
                    monthly_quota_units REAL,
                    period_start TEXT,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(service_provider_id) REFERENCES service_providers(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS registry_models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_provider_id TEXT NOT NULL,
                    model_provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    modality TEXT NOT NULL,
                    tasks TEXT NOT NULL DEFAULT '',
                    capabilities TEXT NOT NULL DEFAULT '',
                    tier TEXT NOT NULL DEFAULT 'cheap',
                    prompt_price REAL NOT NULL DEFAULT 0,
                    completion_price REAL NOT NULL DEFAULT 0,
                    image_price REAL NOT NULL DEFAULT 0,
                    audio_price_per_second REAL NOT NULL DEFAULT 0,
                    character_price REAL NOT NULL DEFAULT 0,
                    cache_read_price REAL NOT NULL DEFAULT 0,
                    cache_write_price REAL NOT NULL DEFAULT 0,
                    web_search_price REAL NOT NULL DEFAULT 0,
                    compute_price_per_second REAL NOT NULL DEFAULT 0,
                    context_length INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'manual',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    pricing_mode TEXT NOT NULL DEFAULT 'per_request',
                    subscription_id INTEGER,
                    last_seen_at DATETIME,
                    deprecated_at DATETIME,
                    replaced_by TEXT,
                    -- Set when a *request* retired the row rather than a sync:
                    -- 'backend' = the provider refused the id it still lists.
                    -- Holds the row retired through catalog syncs (ADR-0010).
                    retired_by TEXT,
                    price_source TEXT,
                    price_source_kind TEXT NOT NULL DEFAULT 'estimate',
                    price_checked_at DATETIME,
                    -- When a request to this pair last came back with an answer.
                    -- The registry keeps confirmed rows for good; the rest are
                    -- candidates a sweep may retire (ADR-0008).
                    confirmed_at DATETIME,
                    -- JSON: attachment role -> this model's own input property.
                    -- Only a human can fill it in; empty means bind positionally.
                    input_map TEXT NOT NULL DEFAULT '{}',
                    -- Comma-joined parameter names this model accepts on top of
                    -- the adapter's declared set. An allowlist, never a
                    -- passthrough; also human-only.
                    extra_params TEXT NOT NULL DEFAULT '',
                    -- JSON: the UTC windows in which every rate on this row is
                    -- multiplied, and by how much (DeepSeek's peak hours).
                    -- Empty means the row costs the same round the clock.
                    price_schedule TEXT NOT NULL DEFAULT '',
                    -- What the model can and cannot do, for humans and for the
                    -- applications choosing among models. Human-written.
                    description TEXT NOT NULL DEFAULT '',
                    -- Bumped on every write. The optimistic lock counts on it
                    -- rather than on updated_at: two edits inside the same
                    -- second carry the same timestamp and the lock would pass.
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE(service_provider_id, model_id),
                    FOREIGN KEY(service_provider_id) REFERENCES service_providers(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_user_id TEXT NOT NULL,
                    service_provider_id TEXT,
                    model_provider TEXT,
                    model_id TEXT,
                    modality TEXT NOT NULL,
                    task TEXT,
                    status TEXT NOT NULL DEFAULT 'ok',
                    error_message TEXT,
                    request_text TEXT,
                    request_ref TEXT,
                    request_json TEXT,
                    response_text TEXT,
                    response_ref TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    audio_seconds REAL,
                    compute_seconds REAL,
                    images INTEGER,
                    characters INTEGER,
                    usage_raw TEXT,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    cost_source TEXT NOT NULL DEFAULT 'computed',
                    pricing_mode TEXT NOT NULL DEFAULT 'per_request',
                    latency_ms INTEGER,
                    finish_reason TEXT,
                    requested_model_id TEXT,
                    fallback_reason TEXT,
                    channel TEXT NOT NULL DEFAULT 'api',
                    tool TEXT,
                    client_id TEXT,
                    client_request_id TEXT,
                    batch_id TEXT,
                    app TEXT,
                    attempts INTEGER,
                    provider_status INTEGER,
                    provider_request_id TEXT,
                    provider_cost_usd REAL,
                    session_id TEXT,
                    return_code INTEGER,
                    plan_json TEXT,
                    finished_at DATETIME,
                    created_at DATETIME NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_registry_models_modality ON registry_models(modality);
                CREATE INDEX IF NOT EXISTS idx_registry_models_tier ON registry_models(tier);
                CREATE INDEX IF NOT EXISTS idx_registry_models_sp ON registry_models(service_provider_id, is_active);
                """
            )
            await conn.commit()
        await self._add_missing_columns()
        await self._create_log_indexes()

    # Indexes over request_logs live after the column migration: some of them
    # cover columns a deployed database only grows in `_add_missing_columns`.
    _LOG_INDEXES = (
        "CREATE INDEX IF NOT EXISTS idx_request_logs_user ON request_logs(external_user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_provider ON request_logs(service_provider_id, created_at)",
        # The console pages the log by descending id and filters on these
        # columns; without the indexes every filtered page is a full scan of a
        # table that only grows.
        "CREATE INDEX IF NOT EXISTS idx_request_logs_id ON request_logs(id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_status ON request_logs(status, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_modality ON request_logs(modality, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_task ON request_logs(task, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_channel ON request_logs(channel, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_model ON request_logs(model_id, id DESC)",
        # Looked up on every run that carries an idempotency key, i.e. on the
        # hot path of the multi-model mode.
        "CREATE INDEX IF NOT EXISTS idx_request_logs_client_request "
        "ON request_logs(client_request_id)",
        # Not on request_logs, but it belongs in this batch rather than in the
        # CREATE TABLE script: on a deployed database the column it covers only
        # appears during `_add_missing_columns`, which runs after that script.
        "CREATE INDEX IF NOT EXISTS idx_registry_models_tasks ON registry_models(tasks)",
    )

    async def _create_log_indexes(self) -> None:
        conn = await self._get_conn()
        async with self._write_lock:
            for statement in self._LOG_INDEXES:
                await conn.execute(statement)
            await conn.commit()

    # Columns added after the first release. `CREATE TABLE IF NOT EXISTS` above
    # covers fresh databases; deployed ones are patched here (SQLite has no
    # `ADD COLUMN IF NOT EXISTS`). Keep both lists in sync.
    _ADDED_COLUMNS: dict[str, dict[str, str]] = {
        "service_providers": {"pricing_url": "TEXT"},
        "registry_models": {
            "last_seen_at": "DATETIME",
            "deprecated_at": "DATETIME",
            "replaced_by": "TEXT",
            "price_source": "TEXT",
            "price_source_kind": "TEXT NOT NULL DEFAULT 'estimate'",
            "price_checked_at": "DATETIME",
            "revision": "INTEGER NOT NULL DEFAULT 0",
            # Wave 2: TTS is billed per character and cached prompt tokens have
            # their own rate; pricing either by the columns that existed was
            # wrong by multiples.
            "character_price": "REAL NOT NULL DEFAULT 0",
            "cache_read_price": "REAL NOT NULL DEFAULT 0",
            "cache_write_price": "REAL NOT NULL DEFAULT 0",
            "web_search_price": "REAL NOT NULL DEFAULT 0",
            # ADR-0006: what the model can be asked to do, as opposed to the
            # group it bills in. Backfilled from `modality` below.
            "tasks": "TEXT NOT NULL DEFAULT ''",
            # Seconds of provider compute, which is not seconds of audio.
            "compute_price_per_second": "REAL NOT NULL DEFAULT 0",
            # ADR-0008: the model id a provider actually answered on. Backfilled
            # from the log below, so history counts as confirmation.
            "confirmed_at": "DATETIME",
            # Which input property each attachment role binds to. Empty on every
            # existing row, which is exactly the positional behaviour they had.
            "input_map": "TEXT NOT NULL DEFAULT '{}'",
            # Which of its own knobs this model lets through. Empty everywhere,
            # which is the adapter-only set every existing row had.
            "extra_params": "TEXT NOT NULL DEFAULT ''",
            # Peak-hour pricing. Empty on every existing row, which is the flat
            # round-the-clock rate they were all billed at, so the migration
            # changes no historical number.
            "price_schedule": "TEXT NOT NULL DEFAULT ''",
            # The model's caveats, as text. Empty on every existing row.
            "description": "TEXT NOT NULL DEFAULT ''",
            # ADR-0010: who retired the row when it was not the catalog. Null on
            # every existing row — all of them were retired by a sync or a hand.
            "retired_by": "TEXT",
        },
        "request_logs": {
            "finish_reason": "TEXT",
            "requested_model_id": "TEXT",
            "fallback_reason": "TEXT",
            # Wave 0 of docs/UI_CONCEPT_v2.md: everything the console has to show
            # but the log never recorded, added in one pass so a deployed
            # database is patched by a single restart.
            "cache_read_tokens": "INTEGER",
            "cache_write_tokens": "INTEGER",
            "reasoning_tokens": "INTEGER",
            "characters": "INTEGER",
            "channel": "TEXT NOT NULL DEFAULT 'api'",
            "tool": "TEXT",
            "client_id": "TEXT",
            "client_request_id": "TEXT",
            # Wave 3: the multi-model mode of «Прогон» sends N runs at once.
            "batch_id": "TEXT",
            # The application the request came from (site, else name). Null on
            # every existing row: nobody identified themselves before the field.
            "app": "TEXT",
            "attempts": "INTEGER",
            "provider_status": "INTEGER",
            "provider_request_id": "TEXT",
            "session_id": "TEXT",
            "return_code": "INTEGER",
            "plan_json": "TEXT",
            "finished_at": "DATETIME",
            # The request side of the inspector: rows written before this column
            # existed keep showing `request_text` alone.
            "request_json": "TEXT",
            # ADR-0006. `modality` is deliberately left exactly as written: it is
            # the only record of what was billed, and the accounting code reads
            # it. `task` is the finer fact, backfilled below.
            "task": "TEXT",
            "compute_seconds": "REAL",
            # The provider's own figure, lifted out of the usage JSON so SQL can
            # compare it with `cost_usd`. Backfilled below.
            "provider_cost_usd": "REAL",
        },
    }

    # `modality` -> the task that value could only have meant. Four are exact;
    # `image` maps to generation because the tool that wrote those rows had no way
    # to send an input image, so no historical row can have meant editing.
    _TASK_BACKFILL: dict[str, str] = {
        "chat": "text-to-text",
        "image": "text-to-image",
        "stt": "speech-to-text",
        "tts": "text-to-speech",
        "cli": "cli",
    }

    async def _add_missing_columns(self) -> None:
        conn = await self._get_conn()
        async with self._write_lock:
            for table, columns in self._ADDED_COLUMNS.items():
                async with conn.execute(f"PRAGMA table_info({table})") as cur:
                    existing = {row["name"] for row in await cur.fetchall()}
                for name, ddl in columns.items():
                    if name not in existing:
                        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                        if table == "registry_models" and name == "price_source_kind":
                            # Rows an admin created already carry a human-checked
                            # price; only the seeded/catalog ones are guesses.
                            await conn.execute(
                                "UPDATE registry_models SET price_source_kind = 'manual' "
                                "WHERE source = 'manual'"
                            )
                        elif table == "registry_models" and name == "tasks":
                            # A registry row can carry several groups (a vision
                            # chat model is tagged chat and image today), so the
                            # comma-joined list is rewritten term by term.
                            for group, task in self._TASK_BACKFILL.items():
                                await conn.execute(
                                    "UPDATE registry_models SET tasks = TRIM("
                                    "  CASE WHEN tasks = '' THEN ? ELSE tasks || ',' || ? END"
                                    ") WHERE (',' || modality || ',') LIKE ?",
                                    (task, task, f"%,{group},%"),
                                )
                        elif table == "request_logs" and name == "provider_cost_usd":
                            # OpenRouter has reported the charged sum all along;
                            # it sat inside the usage JSON where no query could
                            # compare it with what we computed. Old rows join
                            # the reconciliation the moment the column appears.
                            await conn.execute(
                                "UPDATE request_logs SET provider_cost_usd = "
                                "CAST(json_extract(usage_raw, '$.cost') AS REAL) "
                                "WHERE usage_raw IS NOT NULL "
                                "AND json_extract(usage_raw, '$.cost') IS NOT NULL"
                            )
                        elif table == "registry_models" and name == "confirmed_at":
                            # A model that already answered us is confirmed, and
                            # the log is the record of that. Without this backfill
                            # every row on a deployed database would start out
                            # unconfirmed and the first sweep would retire models
                            # in daily use.
                            await conn.execute(
                                """
                                UPDATE registry_models SET confirmed_at = (
                                    SELECT MAX(l.created_at) FROM request_logs l
                                    WHERE l.service_provider_id =
                                              registry_models.service_provider_id
                                      AND l.model_id = registry_models.model_id
                                      AND l.status = 'ok'
                                )
                                """
                            )
                        elif table == "request_logs" and name == "task":
                            # One value per row here, so a plain map is enough.
                            for group, task in self._TASK_BACKFILL.items():
                                await conn.execute(
                                    "UPDATE request_logs SET task = ? WHERE modality = ?",
                                    (task, group),
                                )
            await conn.commit()

    # --- helpers --------------------------------------------------------
    async def _fetchall(self, query: str, params: tuple = ()) -> list[aiosqlite.Row]:
        conn = await self._get_conn()
        async with conn.execute(query, params) as cur:
            return list(await cur.fetchall())

    async def _fetchone(self, query: str, params: tuple = ()) -> aiosqlite.Row | None:
        conn = await self._get_conn()
        async with conn.execute(query, params) as cur:
            return await cur.fetchone()

    # --- service providers ---------------------------------------------
    async def upsert_service_provider(
        self,
        provider_id: str,
        display_name: str,
        adapter: str,
        base_url: str | None = None,
        enabled: bool = True,
        pricing_url: str | None = None,
    ) -> None:
        conn = await self._get_conn()
        async with self._write_lock:
            await conn.execute(
                """
                INSERT INTO service_providers
                    (id, display_name, adapter, enabled, base_url, pricing_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name=excluded.display_name,
                    adapter=excluded.adapter,
                    base_url=excluded.base_url,
                    pricing_url=excluded.pricing_url
                """,
                (
                    provider_id, display_name, adapter, 1 if enabled else 0,
                    base_url, pricing_url, _now(),
                ),
            )
            await conn.commit()

    async def set_provider_enabled(self, provider_id: str, enabled: bool) -> None:
        conn = await self._get_conn()
        async with self._write_lock:
            await conn.execute(
                "UPDATE service_providers SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, provider_id),
            )
            await conn.commit()

    async def get_service_providers(self) -> list[ServiceProviderRecord]:
        rows = await self._fetchall("SELECT * FROM service_providers ORDER BY id")
        return [
            ServiceProviderRecord(
                id=r["id"],
                display_name=r["display_name"],
                adapter=r["adapter"],
                enabled=bool(r["enabled"]),
                base_url=r["base_url"],
                created_at=r["created_at"],
                pricing_url=r["pricing_url"],
            )
            for r in rows
        ]

    async def get_enabled_provider_ids(self) -> set[str]:
        rows = await self._fetchall("SELECT id FROM service_providers WHERE enabled = 1")
        return {r["id"] for r in rows}

    # --- registry models ------------------------------------------------
    async def upsert_registry_model(self, model: RegistryModel) -> None:
        row = _model_to_row(model)
        cols = list(row.keys())
        conn = await self._get_conn()
        async with self._write_lock:
            await conn.execute(
                f"""
                INSERT INTO registry_models ({", ".join(cols)}, created_at, updated_at)
                VALUES ({", ".join("?" for _ in cols)}, ?, ?)
                ON CONFLICT(service_provider_id, model_id) DO UPDATE SET
                    {", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("service_provider_id", "model_id"))},
                    revision=revision + 1,
                    updated_at=excluded.updated_at
                """,
                (*row.values(), _now(), _now()),
            )
            await conn.commit()

    async def seed_registry_model(self, model: RegistryModel) -> bool:
        """Insert a bootstrap seed only if the row was never created.

        Seeds must not resurrect a model the provider has retired (or an admin
        has corrected), so an existing row is left exactly as it is. Returns True
        when a row was actually inserted.

        The human-only columns (`input_map`, `extra_params`, `description`) are
        written here too — on insert only. A sync's upsert leaves them out so it
        cannot erase an operator's edits; a seed inserting a brand-new row is
        the one writer that knows them and cannot overwrite anyone.
        """
        row = _model_to_row(model)
        row["input_map"] = json.dumps(model.input_map, ensure_ascii=False)
        row["extra_params"] = ",".join(model.extra_params)
        row["description"] = model.description
        cols = list(row.keys())
        conn = await self._get_conn()
        async with self._write_lock:
            cur = await conn.execute(
                f"""
                INSERT INTO registry_models ({", ".join(cols)}, created_at, updated_at)
                VALUES ({", ".join("?" for _ in cols)}, ?, ?)
                ON CONFLICT(service_provider_id, model_id) DO NOTHING
                """,
                (*row.values(), _now(), _now()),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def deprecate_registry_model(
        self,
        service_provider_id: str,
        model_id: str,
        *,
        replaced_by: str | None = None,
        retired_by: str | None = None,
    ) -> None:
        """Mark a model as gone from its provider's catalog (never delete it:
        request logs and cost history point at the row).

        `retired_by` names the writer when it is not the catalog sync — today
        only "backend", the provider refusing an id it still lists. That one
        overwrites both the date and the author: the hold `_sync_one` applies is
        counted from the refusal, not from an older retirement the catalog may
        already have undone once.
        """
        stamp = _now()
        conn = await self._get_conn()
        async with self._write_lock:
            if retired_by:
                await conn.execute(
                    """
                    UPDATE registry_models
                    SET is_active = 0,
                        deprecated_at = ?,
                        retired_by = ?,
                        replaced_by = COALESCE(?, replaced_by),
                        revision = revision + 1,
                        updated_at = ?
                    WHERE service_provider_id = ? AND model_id = ?
                    """,
                    (stamp, retired_by, replaced_by, stamp, service_provider_id, model_id),
                )
            else:
                await conn.execute(
                    """
                    UPDATE registry_models
                    SET is_active = 0,
                        deprecated_at = COALESCE(deprecated_at, ?),
                        replaced_by = COALESCE(?, replaced_by),
                        revision = revision + 1,
                        updated_at = ?
                    WHERE service_provider_id = ? AND model_id = ?
                    """,
                    (stamp, replaced_by, stamp, service_provider_id, model_id),
                )
            await conn.commit()

    async def confirm_registry_model(
        self, service_provider_id: str, model_id: str, when: str | None = None
    ) -> None:
        """Record that this pair answered a request.

        Written on every successful dispatch, and cheap on purpose: no revision
        bump, because confirmation is not an edit an operator made and must not
        invalidate the form they have open. A row that is already confirmed keeps
        its first timestamp — «когда подтвердилась» is a fact with one date.
        """
        stamp = when or _now()
        conn = await self._get_conn()
        async with self._write_lock:
            await conn.execute(
                """
                UPDATE registry_models
                SET confirmed_at = COALESCE(confirmed_at, ?), updated_at = ?
                WHERE service_provider_id = ? AND model_id = ?
                """,
                (stamp, _now(), service_provider_id, model_id),
            )
            await conn.commit()

    async def model_ids_in_logs(self, service_provider_id: str | None = None) -> set[str]:
        """Model ids the log mentions at all, successful or not.

        A failed attempt still means somebody chose that model, so a sweep that
        retires "unused" rows must leave these alone: the operator would lose the
        row explaining an error they are still reading.
        """
        query = "SELECT DISTINCT model_id FROM request_logs WHERE model_id IS NOT NULL"
        params: tuple = ()
        if service_provider_id:
            query += " AND service_provider_id = ?"
            params = (service_provider_id,)
        rows = await self._fetchall(query, params)
        return {r["model_id"] for r in rows}

    async def restore_registry_model(self, service_provider_id: str, model_id: str) -> None:
        """Undo a deprecation — the other half of the "few seconds to take it
        back" the console offers after a mass action.

        Only clears the flags a deprecation sets. If the provider really has
        retired the model, the next sync will deprecate it again, which is the
        correct outcome: the registry follows the provider, not the button.
        """
        conn = await self._get_conn()
        async with self._write_lock:
            await conn.execute(
                """
                UPDATE registry_models
                SET is_active = 1, deprecated_at = NULL, retired_by = NULL,
                    revision = revision + 1, updated_at = ?
                WHERE service_provider_id = ? AND model_id = ?
                """,
                (_now(), service_provider_id, model_id),
            )
            await conn.commit()

    async def delete_registry_model(self, service_provider_id: str, model_id: str) -> None:
        conn = await self._get_conn()
        async with self._write_lock:
            await conn.execute(
                "DELETE FROM registry_models WHERE service_provider_id = ? AND model_id = ?",
                (service_provider_id, model_id),
            )
            await conn.commit()

    async def list_registry_models(
        self,
        *,
        modality: str | None = None,
        task: str | None = None,
        tier: str | None = None,
        service_provider_id: str | None = None,
        source: str | None = None,
        active_only: bool = True,
    ) -> list[RegistryModel]:
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("is_active = 1")
        if task:
            # Routing filters on this one. Token-exact even for hyphenated values,
            # so `text-to-text` does not match `image-to-text`.
            clauses.append(_token_clause("tasks"))
            params.extend(_token_params(task))
        if modality:
            # The billing group. Same comma-joined shape as `tasks`.
            clauses.append(_token_clause("modality"))
            params.extend(_token_params(modality))
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        if service_provider_id:
            clauses.append("service_provider_id = ?")
            params.append(service_provider_id)
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._fetchall(
            f"SELECT * FROM registry_models {where} "
            "ORDER BY service_provider_id, model_provider, model_id",
            tuple(params),
        )
        return [_row_to_model(r) for r in rows]

    async def get_registry_model(
        self, service_provider_id: str, model_id: str
    ) -> RegistryModel | None:
        """One row by its pair — the way a model is actually addressed."""
        row = await self._fetchone(
            "SELECT * FROM registry_models WHERE service_provider_id = ? AND model_id = ?",
            (service_provider_id, model_id),
        )
        return _row_to_model(row) if row is not None else None

    # Columns a partial edit is allowed to touch. Everything else — the pair
    # itself, `source`, `is_active`, the catalog dates — belongs to the sync, and
    # the whole-row write that used to serve as "edit" quietly reset all of it.
    _PATCHABLE_MODEL_COLUMNS = frozenset(
        {
            "display_name", "tier", "prompt_price", "completion_price",
            "image_price", "audio_price_per_second", "character_price",
            "cache_read_price", "cache_write_price", "web_search_price",
            "compute_price_per_second",
            "context_length", "pricing_mode", "subscription_id", "price_source",
            "price_source_kind", "price_checked_at",
            # Editable because a model's task set is sometimes only knowable by
            # hand: Replicate publishes an input schema, not a task name.
            "tasks",
            # Same reason, for the input properties: the schema lists
            # `human_img` and `garm_img` and never says which is the garment.
            "input_map",
            # And for this model's own knobs: `scale` on an upscaler is real and
            # useful, and reaching it used to require editing the adapter.
            "extra_params",
            # And for the peak hours: a provider announces them in a mail, not in
            # a catalog, so the only way in is an operator typing them.
            "price_schedule",
            # And for the caveats: what a model cannot do is not in any catalog.
            "description",
        }
    )

    async def update_registry_model(
        self,
        service_provider_id: str,
        model_id: str,
        fields: dict[str, Any],
        *,
        if_revision: int | None = None,
    ) -> RegistryModel | None:
        """Write only the named columns. Returns None if the optimistic lock lost.

        `if_revision` is the revision the operator's copy of the row carried; a
        mismatch means a sync (or another tab) wrote in between, and silently
        overwriting that is how a corrected price disappears.
        """
        unknown = set(fields) - self._PATCHABLE_MODEL_COLUMNS
        if unknown:
            raise ValueError(f"Not patchable: {sorted(unknown)}")
        if not fields:
            return await self.get_registry_model(service_provider_id, model_id)
        assignments = ", ".join(f"{name} = ?" for name in fields)
        params: list[Any] = [*fields.values(), _now(), service_provider_id, model_id]
        guard = ""
        if if_revision is not None:
            guard = " AND revision = ?"
            params.append(if_revision)
        conn = await self._get_conn()
        async with self._write_lock:
            cur = await conn.execute(
                f"UPDATE registry_models SET {assignments}, "
                f"revision = revision + 1, updated_at = ? "
                f"WHERE service_provider_id = ? AND model_id = ?{guard}",
                tuple(params),
            )
            await conn.commit()
            if cur.rowcount == 0:
                return None
        return await self.get_registry_model(service_provider_id, model_id)

    async def get_registry_model_by_id(
        self, model_id: str, *, active_only: bool = True
    ) -> RegistryModel | None:
        """Look up a model by its native id across providers (first match).

        `active_only=False` is how routing tells "never heard of it" apart from
        "the provider retired it", which are different answers for the caller.
        """
        active_clause = "AND is_active = 1" if active_only else ""
        row = await self._fetchone(
            f"SELECT * FROM registry_models WHERE model_id = ? {active_clause} "
            "ORDER BY is_active DESC, service_provider_id LIMIT 1",
            (model_id,),
        )
        return _row_to_model(row) if row is not None else None

    # --- subscriptions (amortization hook) ------------------------------
    async def create_subscription(
        self,
        service_provider_id: str,
        name: str,
        monthly_cost: float,
        monthly_quota_units: float | None = None,
        period_start: str | None = None,
    ) -> int:
        conn = await self._get_conn()
        async with self._write_lock:
            cur = await conn.execute(
                """
                INSERT INTO subscriptions
                    (service_provider_id, name, monthly_cost, monthly_quota_units, period_start, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (service_provider_id, name, monthly_cost, monthly_quota_units, period_start, _now()),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def get_subscription(self, subscription_id: int) -> SubscriptionRecord | None:
        row = await self._fetchone(
            "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
        )
        if row is None:
            return None
        return SubscriptionRecord(
            id=row["id"],
            service_provider_id=row["service_provider_id"],
            name=row["name"],
            monthly_cost=row["monthly_cost"],
            monthly_quota_units=row["monthly_quota_units"],
            period_start=row["period_start"],
            created_at=row["created_at"],
        )

    # --- request logging ------------------------------------------------
    # Every scalar field of RequestLogRecord maps 1:1 to a column; usage_raw is
    # the one that needs encoding. Deriving the INSERT from the dataclass keeps
    # a new field from being silently dropped on the way to disk.
    _LOG_ENCODED_FIELDS = {
        "usage_raw": lambda v: json.dumps(v, ensure_ascii=False) if v else None,
        "request_json": lambda v: json.dumps(v, ensure_ascii=False) if v else None,
    }

    def _log_values(self, rec: RequestLogRecord) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name in RequestLogRecord.__dataclass_fields__:
            value = getattr(rec, name)
            encode = self._LOG_ENCODED_FIELDS.get(name)
            values[name] = encode(value) if encode else value
        return values

    async def log_request(self, rec: RequestLogRecord) -> int:
        values = self._log_values(rec)
        values["created_at"] = _now()
        cols = list(values)
        conn = await self._get_conn()
        async with self._write_lock:
            cur = await conn.execute(
                f"INSERT INTO request_logs ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                tuple(values.values()),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def finish_request_log(self, log_id: int, rec: RequestLogRecord) -> None:
        """Write the outcome onto a row that was opened as `running`.

        The row is created before the provider is called so a run in flight is
        visible in the log and survives a restart as `interrupted`; this is the
        second half of that write, and it sets every column the finished record
        carries except `created_at`, which belongs to the moment the run started.
        """
        values = self._log_values(rec)
        assignments = ", ".join(f"{name} = ?" for name in values)
        conn = await self._get_conn()
        async with self._write_lock:
            await conn.execute(
                f"UPDATE request_logs SET {assignments} WHERE id = ?",
                (*values.values(), log_id),
            )
            await conn.commit()

    # Statuses that mean "this run is still going". Nothing outside the process
    # that started it can move them along, which is why a restart has to.
    LIVE_STATUSES = ("running", "queued")

    async def interrupt_live_logs(self) -> int:
        """Close out runs the previous process was executing when it stopped.

        Without this a killed service leaves rows reading «выполняется» forever,
        and the console cannot tell them from a call that is genuinely in flight.
        The outcome is honestly unknown: the provider may well have run and
        billed the request, so the cost source says so rather than claiming zero.
        """
        placeholders = ", ".join("?" for _ in self.LIVE_STATUSES)
        conn = await self._get_conn()
        async with self._write_lock:
            cur = await conn.execute(
                "UPDATE request_logs SET status = 'interrupted', "
                "cost_source = 'cost_unknown', finished_at = ?, "
                "error_message = COALESCE(error_message, "
                "'Сервис перезапустился, пока запрос выполнялся') "
                f"WHERE status IN ({placeholders})",
                (_now(), *self.LIVE_STATUSES),
            )
            await conn.commit()
            return cur.rowcount or 0

    async def find_log_by_client_request_id(
        self, client_request_id: str
    ) -> dict[str, Any] | None:
        """The row a repeated `client_request_id` already produced.

        Idempotency is the point: a double-submitted form, or a client retrying
        after a dropped connection, must not spend the money twice.
        """
        row = await self._fetchone(
            "SELECT * FROM request_logs WHERE client_request_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (client_request_id,),
        )
        return dict(row) if row is not None else None

    # What the log table on screen actually shows. The full row carries the
    # response text (up to 4000 chars) and the raw usage block; sending fifty of
    # those to draw a table of numbers is the difference between a 6 KB and a
    # 300 KB response.
    LOG_LIST_COLUMNS = (
        "id", "created_at", "external_user_id", "modality", "task", "service_provider_id",
        "model_provider", "model_id", "requested_model_id", "fallback_reason",
        "status", "error_message", "channel", "tool", "client_id", "batch_id", "app",
        "prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens",
        "reasoning_tokens", "audio_seconds", "compute_seconds", "images", "characters",
        "cost_usd", "cost_source", "pricing_mode", "provider_cost_usd",
        "latency_ms", "attempts", "finish_reason", "request_text",
    )

    _LOG_UNVERIFIED_COST = ("estimated", "unpriced", "cost_unknown", "provider_notional")

    # When our arithmetic and the provider's charge count as "разошлись":
    # more than 2% apart and more than a hundredth of a cent — the second
    # bound keeps float dust out of the work list without hiding a systematic
    # miss on cheap requests (a $0.001 run billed as $0.002 must still flag).
    # Sources where the two figures are one number by construction (the
    # provider's sum *is* the cost) are excluded in the clause itself.
    _COST_MISMATCH_REL = 0.02
    _COST_MISMATCH_ABS = 0.0001

    def _log_filters(
        self,
        *,
        external_user_id: str | None,
        status: str | None,
        modality: str | None,
        task: str | None,
        model_id: str | None,
        channel: str | None,
        batch_id: str | None,
        since: str | None,
        until: str | None,
        with_fallback: bool,
        with_retries: bool,
        unverified_cost: bool,
        cost_mismatch: bool = False,
        user_contains: str | None = None,
        model_contains: str | None = None,
        provider: str | None = None,
        app_contains: str | None = None,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if external_user_id:
            clauses.append("external_user_id = ?")
            params.append(external_user_id)
        # Substring, not equality: the console's user box is a search field, and
        # «ort-bot» must find every ort-bot-XXXX. `instr` instead of LIKE so a
        # legitimate % or _ in a label needs no escaping.
        if user_contains:
            clauses.append("instr(lower(external_user_id), lower(?)) > 0")
            params.append(user_contains)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if modality:
            clauses.append("modality = ?")
            params.append(modality)
        if task:
            clauses.append("task = ?")
            params.append(task)
        if model_id:
            clauses.append("model_id = ?")
            params.append(model_id)
        if model_contains:
            clauses.append("instr(lower(model_id), lower(?)) > 0")
            params.append(model_contains)
        if provider:
            clauses.append("service_provider_id = ?")
            params.append(provider)
        # Substring like the user box: «wildcar» finds https://wildcar.org and
        # every subdomain of it.
        if app_contains:
            clauses.append("instr(lower(app), lower(?)) > 0")
            params.append(app_contains)
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if batch_id:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        if with_fallback:
            clauses.append("requested_model_id IS NOT NULL")
        if with_retries:
            clauses.append("attempts > 1")
        if unverified_cost:
            placeholders = ", ".join("?" for _ in self._LOG_UNVERIFIED_COST)
            clauses.append(f"cost_source IN ({placeholders})")
            params.extend(self._LOG_UNVERIFIED_COST)
        if cost_mismatch:
            # Rows where the provider named a sum and our arithmetic landed
            # somewhere else. `provider`/`provider_notional` rows are excluded:
            # there the recorded cost *is* the provider's figure, and amortized
            # rows spread a subscription — neither comparison means anything.
            clauses.append(
                "provider_cost_usd IS NOT NULL AND provider_cost_usd > 0 "
                "AND cost_source NOT IN ('provider', 'provider_notional', 'amortized') "
                "AND ABS(cost_usd - provider_cost_usd) > MAX(?, ? * provider_cost_usd)"
            )
            params.extend([self._COST_MISMATCH_ABS, self._COST_MISMATCH_REL])
        return clauses, params

    async def list_request_logs(
        self,
        *,
        external_user_id: str | None = None,
        status: str | None = None,
        modality: str | None = None,
        task: str | None = None,
        model_id: str | None = None,
        channel: str | None = None,
        batch_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        with_fallback: bool = False,
        with_retries: bool = False,
        unverified_cost: bool = False,
        cost_mismatch: bool = False,
        user_contains: str | None = None,
        model_contains: str | None = None,
        provider: str | None = None,
        app_contains: str | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """One page of the log, newest first, projected to the table columns.

        Pagination is by cursor (`before_id`), not offset: the log grows from the
        head, so page two of an offset query would repeat rows from page one.
        """
        clauses, params = self._log_filters(
            external_user_id=external_user_id,
            status=status,
            modality=modality,
            task=task,
            model_id=model_id,
            channel=channel,
            batch_id=batch_id,
            since=since,
            until=until,
            with_fallback=with_fallback,
            with_retries=with_retries,
            unverified_cost=unverified_cost,
            cost_mismatch=cost_mismatch,
            user_contains=user_contains,
            model_contains=model_contains,
            provider=provider,
            app_contains=app_contains,
        )
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = await self._fetchall(
            f"SELECT {', '.join(self.LOG_LIST_COLUMNS)} FROM request_logs {where} "
            "ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
        return [dict(r) for r in rows]

    async def count_request_logs(
        self,
        *,
        external_user_id: str | None = None,
        status: str | None = None,
        modality: str | None = None,
        task: str | None = None,
        model_id: str | None = None,
        channel: str | None = None,
        batch_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        with_fallback: bool = False,
        with_retries: bool = False,
        unverified_cost: bool = False,
        cost_mismatch: bool = False,
        user_contains: str | None = None,
        model_contains: str | None = None,
        provider: str | None = None,
        app_contains: str | None = None,
    ) -> int:
        clauses, params = self._log_filters(
            external_user_id=external_user_id,
            status=status,
            modality=modality,
            task=task,
            model_id=model_id,
            channel=channel,
            batch_id=batch_id,
            since=since,
            until=until,
            with_fallback=with_fallback,
            with_retries=with_retries,
            unverified_cost=unverified_cost,
            cost_mismatch=cost_mismatch,
            user_contains=user_contains,
            model_contains=model_contains,
            provider=provider,
            app_contains=app_contains,
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await self._fetchone(
            f"SELECT COUNT(*) AS n FROM request_logs {where}", tuple(params)
        )
        return int(row["n"]) if row else 0

    # Which columns the console may ask suggestions for. A closed map, not
    # string interpolation: the field name goes straight into SQL.
    LOG_SUGGEST_FIELDS = {
        "user": "external_user_id",
        "model": "model_id",
        "provider": "service_provider_id",
        "app": "app",
    }

    async def suggest_log_values(
        self, field: str, *, contains: str = "", limit: int = 20
    ) -> list[str]:
        """Distinct values one log column has seen, freshest first.

        Feeds the filter dropdowns and the as-you-type подсказки: the list of
        users, models or providers worth offering is exactly the list the log
        already contains, not what the registry thinks exists today.
        """
        column = self.LOG_SUGGEST_FIELDS.get(field)
        if column is None:
            raise ValueError(f"Unknown suggest field {field!r}")
        clauses = [f"{column} IS NOT NULL"]
        params: list[Any] = []
        if contains:
            clauses.append(f"instr(lower({column}), lower(?)) > 0")
            params.append(contains)
        rows = await self._fetchall(
            f"SELECT {column} AS value FROM request_logs "
            f"WHERE {' AND '.join(clauses)} "
            f"GROUP BY {column} ORDER BY MAX(id) DESC LIMIT ?",
            tuple(params + [limit]),
        )
        return [str(r["value"]) for r in rows]

    # --- retention --------------------------------------------------------
    async def count_logs_before(self, before: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM request_logs WHERE created_at < ?", (before,)
        )
        return int(row["n"]) if row else 0

    async def list_refs_before(self, before: str) -> list[str]:
        """Blob refs held only by rows that are about to be deleted.

        Collected before the delete, because afterwards nothing remembers which
        files those rows pointed at and the blobs become orphans on disk. A ref
        that any surviving row still names is left out.
        """
        rows = await self._fetchall(
            "SELECT DISTINCT ref FROM ("
            "  SELECT request_ref AS ref FROM request_logs WHERE created_at < ?"
            "  UNION SELECT response_ref FROM request_logs WHERE created_at < ?"
            ") WHERE ref IS NOT NULL AND ref NOT IN ("
            "  SELECT request_ref FROM request_logs WHERE created_at >= ? "
            "     AND request_ref IS NOT NULL"
            "  UNION SELECT response_ref FROM request_logs WHERE created_at >= ? "
            "     AND response_ref IS NOT NULL"
            ")",
            (before, before, before, before),
        )
        return [str(r["ref"]) for r in rows]

    async def all_referenced_refs(self) -> set[str]:
        """Every blob ref the log still points at, for orphan collection."""
        rows = await self._fetchall(
            "SELECT DISTINCT request_ref AS ref FROM request_logs "
            "WHERE request_ref IS NOT NULL "
            "UNION SELECT DISTINCT response_ref FROM request_logs "
            "WHERE response_ref IS NOT NULL"
        )
        return {str(r["ref"]) for r in rows}

    async def delete_logs_before(self, before: str) -> int:
        conn = await self._get_conn()
        async with self._write_lock:
            cur = await conn.execute(
                "DELETE FROM request_logs WHERE created_at < ?", (before,)
            )
            await conn.commit()
            return cur.rowcount or 0

    async def get_request_log(self, log_id: int) -> dict[str, Any] | None:
        """The full row, including response text and raw usage — what the
        inspector needs and the list deliberately does not carry."""
        row = await self._fetchone("SELECT * FROM request_logs WHERE id = ?", (log_id,))
        return dict(row) if row is not None else None

    # What a spend row can be grouped by. A closed map, not string
    # interpolation: these go straight into GROUP BY.
    USAGE_GROUPINGS = {
        "user": "external_user_id",
        "model": "model_id",
        "provider": "service_provider_id",
        "channel": "channel",
        "app": "app",
        "day": "substr(created_at, 1, 10)",
    }

    # Sums that must never be added together. Real money, money that was never
    # charged, and requests whose cost nobody can state are three different
    # facts, and one total hides all three (§7.2, §7.3).
    _CONFIRMED_SOURCES = ("computed", "provider", "manual")
    _UNCONFIRMED_SOURCES = ("estimated",)
    _NOTIONAL_SOURCES = ("provider_notional",)
    _NO_METHOD_SOURCES = ("unpriced",)
    _UNKNOWN_SOURCES = ("cost_unknown",)

    async def get_spend(
        self,
        *,
        group_by: str = "user",
        external_user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Spend grouped one way, plus the totals split by how much to trust them.

        Returns `(rows, totals)`. `totals` keeps confirmed, unconfirmed and
        notional money apart and counts the requests nobody can price, so the
        screen can label the sum a lower bound instead of a fact.
        """
        column = self.USAGE_GROUPINGS.get(group_by)
        if column is None:
            raise ValueError(f"Unknown grouping {group_by!r}")

        clauses: list[str] = []
        params: list[Any] = []
        if external_user_id:
            clauses.append("external_user_id = ?")
            params.append(external_user_id)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        def _sum(sources: tuple[str, ...]) -> str:
            listed = ", ".join(f"'{s}'" for s in sources)
            return f"COALESCE(SUM(CASE WHEN cost_source IN ({listed}) THEN cost_usd END), 0)"

        def _count(sources: tuple[str, ...]) -> str:
            listed = ", ".join(f"'{s}'" for s in sources)
            return f"SUM(CASE WHEN cost_source IN ({listed}) THEN 1 ELSE 0 END)"

        select = f"""
            SELECT {column} AS bucket,
                   COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                   {_sum(self._CONFIRMED_SOURCES)} AS confirmed_usd,
                   {_sum(self._UNCONFIRMED_SOURCES)} AS unconfirmed_usd,
                   {_sum(self._NOTIONAL_SOURCES)} AS notional_usd,
                   {_count(self._NO_METHOD_SOURCES)} AS unpriced_requests,
                   {_count(self._UNKNOWN_SOURCES)} AS unknown_requests,
                   SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS errors
            FROM request_logs
            {where}
            GROUP BY bucket
        """
        rows = await self._fetchall(select, tuple(params))
        items = [dict(r) for r in rows]
        for item in items:
            money = item["confirmed_usd"] + item["unconfirmed_usd"]
            item["total_usd"] = money
            item["confirmed_share"] = (
                item["confirmed_usd"] / money if money else None
            )
        items.sort(key=lambda r: r["total_usd"], reverse=True)

        totals = {
            "requests": sum(r["requests"] for r in items),
            "total_tokens": sum(r["total_tokens"] for r in items),
            "confirmed_usd": sum(r["confirmed_usd"] for r in items),
            "unconfirmed_usd": sum(r["unconfirmed_usd"] for r in items),
            "notional_usd": sum(r["notional_usd"] for r in items),
            "unpriced_requests": sum(r["unpriced_requests"] for r in items),
            "unknown_requests": sum(r["unknown_requests"] for r in items),
            "errors": sum(r["errors"] for r in items),
        }
        totals["total_usd"] = totals["confirmed_usd"] + totals["unconfirmed_usd"]
        # With unpriced or timed-out requests in the selection the sum is a floor
        # and nothing else, and the screen has to say so.
        totals["is_lower_bound"] = bool(
            totals["unpriced_requests"] or totals["unknown_requests"]
        )
        return items, totals

    async def get_tool_stats(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """Calls per MCP tool: how many, how many failed, and the median latency.

        SQLite has no percentile function, so p50 is taken as the middle element
        of the ordered latencies per tool. On the volumes this console deals
        with that is exact rather than an approximation.
        """
        clauses = ["channel = 'mcp'", "tool IS NOT NULL"]
        params: list[Any] = []
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = " AND ".join(clauses)
        rows = await self._fetchall(
            f"""
            SELECT tool,
                   COUNT(*) AS calls,
                   SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS errors
            FROM request_logs WHERE {where}
            GROUP BY tool ORDER BY calls DESC
            """,
            tuple(params),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            latencies = await self._fetchall(
                f"SELECT latency_ms FROM request_logs "
                f"WHERE {where} AND tool = ? AND latency_ms IS NOT NULL "
                "ORDER BY latency_ms",
                (*params, row["tool"]),
            )
            values = [r["latency_ms"] for r in latencies]
            out.append(
                {
                    "tool": row["tool"],
                    "calls": row["calls"],
                    "errors": row["errors"],
                    "p50_ms": values[len(values) // 2] if values else None,
                }
            )
        return out

    async def get_client_stats(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """Who has been calling, by the label the client sent.

        The label is caller-supplied and unverified — a grouping key exactly like
        `external_user_id`, never a trust boundary. Telling clients apart for
        real needs named tokens, which is an open question in §24.
        """
        clauses = ["channel = 'mcp'"]
        params: list[Any] = []
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = " AND ".join(clauses)
        rows = await self._fetchall(
            f"""
            SELECT COALESCE(client_id, '(без метки)') AS client,
                   COUNT(*) AS calls,
                   MAX(created_at) AS last_call_at,
                   SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS errors
            FROM request_logs WHERE {where}
            GROUP BY client ORDER BY calls DESC
            """,
            tuple(params),
        )
        return [dict(r) for r in rows]

    async def get_usage(
        self, *, external_user_id: str | None = None, since: str | None = None
    ) -> list[UsageRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if external_user_id:
            clauses.append("external_user_id = ?")
            params.append(external_user_id)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._fetchall(
            f"""
            SELECT external_user_id,
                   COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_usd), 0) AS total_cost_usd
            FROM request_logs
            {where}
            GROUP BY external_user_id
            ORDER BY total_cost_usd DESC
            """,
            tuple(params),
        )
        return [
            UsageRow(
                external_user_id=r["external_user_id"],
                requests=r["requests"],
                total_tokens=r["total_tokens"],
                total_cost_usd=r["total_cost_usd"],
            )
            for r in rows
        ]
