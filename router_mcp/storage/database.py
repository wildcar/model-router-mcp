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
    audio_seconds: float | None = None
    images: int | None = None
    usage_raw: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    cost_source: str = "computed"  # "computed" | "manual" | "amortized" | "provider"
    pricing_mode: str = "per_request"
    latency_ms: int | None = None


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
                    audio_seconds REAL,
                    images INTEGER,
                    usage_raw TEXT,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    cost_source TEXT NOT NULL DEFAULT 'computed',
                    pricing_mode TEXT NOT NULL DEFAULT 'per_request',
                    latency_ms INTEGER,
                    created_at DATETIME NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_registry_models_modality ON registry_models(modality);
                CREATE INDEX IF NOT EXISTS idx_registry_models_tier ON registry_models(tier);
                CREATE INDEX IF NOT EXISTS idx_registry_models_sp ON registry_models(service_provider_id, is_active);
                CREATE INDEX IF NOT EXISTS idx_request_logs_user ON request_logs(external_user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_request_logs_provider ON request_logs(service_provider_id, created_at);
                """
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
    ) -> None:
        conn = await self._get_conn()
        async with self._write_lock:
            await conn.execute(
                """
                INSERT INTO service_providers (id, display_name, adapter, enabled, base_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name=excluded.display_name,
                    adapter=excluded.adapter,
                    base_url=excluded.base_url
                """,
                (provider_id, display_name, adapter, 1 if enabled else 0, base_url, _now()),
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
                    updated_at=excluded.updated_at
                """,
                (*row.values(), _now(), _now()),
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

    async def get_registry_model_by_id(self, model_id: str) -> RegistryModel | None:
        """Look up a model by its native id across providers (first active match)."""
        row = await self._fetchone(
            "SELECT * FROM registry_models WHERE model_id = ? AND is_active = 1 "
            "ORDER BY service_provider_id LIMIT 1",
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
    async def log_request(self, rec: RequestLogRecord) -> int:
        conn = await self._get_conn()
        async with self._write_lock:
            cur = await conn.execute(
                """
                INSERT INTO request_logs (
                    external_user_id, service_provider_id, model_provider, model_id,
                    modality, status, error_message, request_text, request_ref,
                    response_text, response_ref, prompt_tokens, completion_tokens,
                    total_tokens, audio_seconds, images, usage_raw, cost_usd,
                    cost_source, pricing_mode, latency_ms, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rec.external_user_id,
                    rec.service_provider_id,
                    rec.model_provider,
                    rec.model_id,
                    rec.modality,
                    rec.status,
                    rec.error_message,
                    rec.request_text,
                    rec.request_ref,
                    rec.response_text,
                    rec.response_ref,
                    rec.prompt_tokens,
                    rec.completion_tokens,
                    rec.total_tokens,
                    rec.audio_seconds,
                    rec.images,
                    json.dumps(rec.usage_raw, ensure_ascii=False) if rec.usage_raw else None,
                    rec.cost_usd,
                    rec.cost_source,
                    rec.pricing_mode,
                    rec.latency_ms,
                    _now(),
                ),
            )
            await conn.commit()
            return int(cur.lastrowid)

    async def list_request_logs(
        self,
        *,
        external_user_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if external_user_id:
            clauses.append("external_user_id = ?")
            params.append(external_user_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = await self._fetchall(
            f"SELECT * FROM request_logs {where} ORDER BY id DESC LIMIT ?",
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
