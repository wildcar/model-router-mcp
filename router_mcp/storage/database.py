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
from router_mcp.registry.models import RegistryModel


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
    modality: str
    service_provider_id: str | None = None
    model_provider: str | None = None
    model_id: str | None = None
    status: str = "ok"
    error_message: str | None = None
    request_text: str | None = None
    request_ref: str | None = None
    response_text: str | None = None
    response_ref: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    audio_seconds: float | None = None
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
    # Execution detail the console's timeline and error card need.
    attempts: int | None = None  # HTTP tries inside the successful provider attempt
    provider_status: int | None = None  # last upstream HTTP status
    provider_request_id: str | None = None  # upstream id, for support tickets
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


def _model_to_row(m: RegistryModel) -> dict[str, Any]:
    return {
        "service_provider_id": m.service_provider_id,
        "model_provider": m.model_provider,
        "model_id": m.model_id,
        "display_name": m.display_name,
        "modality": ",".join(mod.value for mod in m.modalities),
        "capabilities": ",".join(m.capabilities),
        "tier": m.tier,
        "prompt_price": m.prompt_price,
        "completion_price": m.completion_price,
        "image_price": m.image_price,
        "audio_price_per_second": m.audio_price_per_second,
        "context_length": m.context_length,
        "source": m.source,
        "is_active": 1 if m.is_active else 0,
        "pricing_mode": m.pricing_mode,
        "subscription_id": m.subscription_id,
        "last_seen_at": m.last_seen_at,
        "deprecated_at": m.deprecated_at,
        "replaced_by": m.replaced_by,
        "price_source": m.price_source,
        "price_source_kind": m.price_source_kind,
        "price_checked_at": m.price_checked_at,
    }


def _row_to_model(row: aiosqlite.Row) -> RegistryModel:
    modalities = tuple(
        Modality.from_str(part)
        for part in str(row["modality"]).split(",")
        if part.strip()
    )
    caps = tuple(part for part in str(row["capabilities"]).split(",") if part.strip())
    return RegistryModel(
        service_provider_id=row["service_provider_id"],
        model_provider=row["model_provider"],
        model_id=row["model_id"],
        display_name=row["display_name"],
        modalities=modalities,
        capabilities=caps,
        tier=row["tier"],
        prompt_price=row["prompt_price"],
        completion_price=row["completion_price"],
        image_price=row["image_price"],
        audio_price_per_second=row["audio_price_per_second"],
        context_length=row["context_length"],
        source=row["source"],
        is_active=bool(row["is_active"]),
        pricing_mode=row["pricing_mode"],
        subscription_id=row["subscription_id"],
        last_seen_at=row["last_seen_at"],
        deprecated_at=row["deprecated_at"],
        replaced_by=row["replaced_by"],
        price_source=row["price_source"],
        price_source_kind=row["price_source_kind"] or "estimate",
        price_checked_at=row["price_checked_at"],
        updated_at=row["updated_at"],
        revision=row["revision"],
    )


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
                    capabilities TEXT NOT NULL DEFAULT '',
                    tier TEXT NOT NULL DEFAULT 'cheap',
                    prompt_price REAL NOT NULL DEFAULT 0,
                    completion_price REAL NOT NULL DEFAULT 0,
                    image_price REAL NOT NULL DEFAULT 0,
                    audio_price_per_second REAL NOT NULL DEFAULT 0,
                    context_length INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'manual',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    pricing_mode TEXT NOT NULL DEFAULT 'per_request',
                    subscription_id INTEGER,
                    last_seen_at DATETIME,
                    deprecated_at DATETIME,
                    replaced_by TEXT,
                    price_source TEXT,
                    price_source_kind TEXT NOT NULL DEFAULT 'estimate',
                    price_checked_at DATETIME,
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
                    status TEXT NOT NULL DEFAULT 'ok',
                    error_message TEXT,
                    request_text TEXT,
                    request_ref TEXT,
                    response_text TEXT,
                    response_ref TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    audio_seconds REAL,
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
                    attempts INTEGER,
                    provider_status INTEGER,
                    provider_request_id TEXT,
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
        "CREATE INDEX IF NOT EXISTS idx_request_logs_channel ON request_logs(channel, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_request_logs_model ON request_logs(model_id, id DESC)",
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
            "attempts": "INTEGER",
            "provider_status": "INTEGER",
            "provider_request_id": "TEXT",
            "session_id": "TEXT",
            "return_code": "INTEGER",
            "plan_json": "TEXT",
            "finished_at": "DATETIME",
        },
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
        """
        row = _model_to_row(model)
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
        self, service_provider_id: str, model_id: str, *, replaced_by: str | None = None
    ) -> None:
        """Mark a model as gone from its provider's catalog (never delete it:
        request logs and cost history point at the row)."""
        conn = await self._get_conn()
        async with self._write_lock:
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
                (_now(), replaced_by, _now(), service_provider_id, model_id),
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
        tier: str | None = None,
        service_provider_id: str | None = None,
        source: str | None = None,
        active_only: bool = True,
    ) -> list[RegistryModel]:
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("is_active = 1")
        if modality:
            # modality column is a comma-joined list; match a whole token.
            clauses.append(
                "(modality = ? OR modality LIKE ? OR modality LIKE ? OR modality LIKE ?)"
            )
            params.extend([modality, f"{modality},%", f"%,{modality}", f"%,{modality},%"])
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
            "image_price", "audio_price_per_second", "context_length",
            "pricing_mode", "subscription_id", "price_source",
            "price_source_kind", "price_checked_at",
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
    }

    async def log_request(self, rec: RequestLogRecord) -> int:
        values: dict[str, Any] = {}
        for name in RequestLogRecord.__dataclass_fields__:
            value = getattr(rec, name)
            encode = self._LOG_ENCODED_FIELDS.get(name)
            values[name] = encode(value) if encode else value
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

    # What the log table on screen actually shows. The full row carries the
    # response text (up to 4000 chars) and the raw usage block; sending fifty of
    # those to draw a table of numbers is the difference between a 6 KB and a
    # 300 KB response.
    LOG_LIST_COLUMNS = (
        "id", "created_at", "external_user_id", "modality", "service_provider_id",
        "model_provider", "model_id", "requested_model_id", "fallback_reason",
        "status", "error_message", "channel", "tool", "client_id",
        "prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens",
        "reasoning_tokens", "audio_seconds", "images", "characters",
        "cost_usd", "cost_source", "pricing_mode",
        "latency_ms", "attempts", "finish_reason", "request_text",
    )

    _LOG_UNVERIFIED_COST = ("estimated", "unpriced", "cost_unknown", "provider_notional")

    def _log_filters(
        self,
        *,
        external_user_id: str | None,
        status: str | None,
        modality: str | None,
        model_id: str | None,
        channel: str | None,
        since: str | None,
        until: str | None,
        with_fallback: bool,
        with_retries: bool,
        unverified_cost: bool,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if external_user_id:
            clauses.append("external_user_id = ?")
            params.append(external_user_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if modality:
            clauses.append("modality = ?")
            params.append(modality)
        if model_id:
            clauses.append("model_id = ?")
            params.append(model_id)
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
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
        return clauses, params

    async def list_request_logs(
        self,
        *,
        external_user_id: str | None = None,
        status: str | None = None,
        modality: str | None = None,
        model_id: str | None = None,
        channel: str | None = None,
        since: str | None = None,
        until: str | None = None,
        with_fallback: bool = False,
        with_retries: bool = False,
        unverified_cost: bool = False,
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
            model_id=model_id,
            channel=channel,
            since=since,
            until=until,
            with_fallback=with_fallback,
            with_retries=with_retries,
            unverified_cost=unverified_cost,
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
        model_id: str | None = None,
        channel: str | None = None,
        since: str | None = None,
        until: str | None = None,
        with_fallback: bool = False,
        with_retries: bool = False,
        unverified_cost: bool = False,
    ) -> int:
        clauses, params = self._log_filters(
            external_user_id=external_user_id,
            status=status,
            modality=modality,
            model_id=model_id,
            channel=channel,
            since=since,
            until=until,
            with_fallback=with_fallback,
            with_retries=with_retries,
            unverified_cost=unverified_cost,
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await self._fetchone(
            f"SELECT COUNT(*) AS n FROM request_logs {where}", tuple(params)
        )
        return int(row["n"]) if row else 0

    async def get_request_log(self, log_id: int) -> dict[str, Any] | None:
        """The full row, including response text and raw usage — what the
        inspector needs and the list deliberately does not carry."""
        row = await self._fetchone("SELECT * FROM request_logs WHERE id = ?", (log_id,))
        return dict(row) if row is not None else None

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
