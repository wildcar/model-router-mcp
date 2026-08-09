"""The closed health summary and the live provider probe.

`/health` stays public and says only that the service is alive. Everything with
substance — version, provider inventory, disk usage, catalog freshness — is here
instead, behind the same gate as the rest of the console: a map of the
installation is not something an open endpoint should hand out (§16.5).

The probe exists because `configured` answers "is there a key", not "does the
key still work". A revoked key reads as healthy until something is actually
sent, so the probe sends the cheapest thing each provider offers and caches the
answer — checking on every page load would spend the rate limit on reassurance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from router_mcp.domain.errors import AccessDenied, NotFound, RouterError
from router_mcp.web import public_blobs

# How long a probe result is trusted. Long enough that opening the screen
# repeatedly costs nothing, short enough that a revoked key surfaces the same
# working day.
PROBE_TTL_SECONDS = 600


@dataclass(slots=True)
class ProbeResult:
    provider_id: str
    status: str  # "ok" | "unauthorized" | "unavailable" | "not_configured" | "no_probe"
    detail: str
    latency_ms: int | None
    checked_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "age_seconds": int(time.time() - self.checked_at),
        }


_probe_cache: dict[str, ProbeResult] = {}


async def probe_provider(core, provider_id: str, *, force: bool = False) -> ProbeResult:
    """Ask a provider whether the key we hold still works."""
    adapter = core.adapters.get(provider_id)
    if adapter is None:
        raise NotFound(f"No adapter for provider {provider_id!r}")

    cached = _probe_cache.get(provider_id)
    if cached and not force and time.time() - cached.checked_at < PROBE_TTL_SECONDS:
        return cached

    started = time.monotonic()
    if not adapter.is_configured():
        result = ProbeResult(
            provider_id, "not_configured",
            f"Переменная {adapter.env_var} не задана" if adapter.env_var
            else "Адаптер не настроен",
            None, time.time(),
        )
        _probe_cache[provider_id] = result
        return result

    # The catalog call is the cheapest authenticated request most providers
    # have. Adapters without one (CLI engines) can only report that the binary
    # resolves, which `is_configured` already established.
    try:
        catalog = await adapter.list_catalog()
    except AccessDenied as exc:
        result = ProbeResult(
            provider_id, "unauthorized", f"Ключ отклонён: {exc}",
            int((time.monotonic() - started) * 1000), time.time(),
        )
    except RouterError as exc:
        result = ProbeResult(
            provider_id, "unavailable", str(exc),
            int((time.monotonic() - started) * 1000), time.time(),
        )
    except Exception as exc:  # a broken provider must not break the screen
        result = ProbeResult(
            provider_id, "unavailable", f"{type(exc).__name__}: {exc}",
            int((time.monotonic() - started) * 1000), time.time(),
        )
    else:
        latency = int((time.monotonic() - started) * 1000)
        if catalog is None:
            result = ProbeResult(
                provider_id, "no_probe",
                "У поставщика нет каталога: живую проверку сделать нечем",
                latency, time.time(),
            )
        else:
            result = ProbeResult(
                provider_id, "ok", f"Каталог ответил, моделей {len(catalog)}",
                latency, time.time(),
            )
    _probe_cache[provider_id] = result
    return result


def cached_probe(provider_id: str) -> dict[str, Any] | None:
    result = _probe_cache.get(provider_id)
    return result.as_dict() if result else None


def _dir_size(path: str | None) -> int:
    if not path:
        return 0
    root = Path(path)
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


async def summary(core, started_at: float) -> dict[str, Any]:
    """Everything «Обзор» shows, in one call."""
    from router_mcp import __version__

    providers = await core.db.get_service_providers()
    enabled = await core.db.get_enabled_provider_ids()
    models = await core.db.list_registry_models(active_only=False)
    active = [m for m in models if m.is_active]

    unpriced = [m for m in active if m.price_is_unknown]
    unverified = [m for m in active if not m.price_is_verified and not m.price_is_unknown]

    since = _hours_ago(24)
    _rows, day_totals = await core.db.get_spend(group_by="day", since=since)
    freshness = max((m.last_seen_at or "" for m in active), default="")

    db_bytes = Path(core.settings.db_path).stat().st_size if Path(core.settings.db_path).exists() else 0
    blob_bytes = _dir_size(core.settings.blob_dir)

    provider_rows = []
    for p in providers:
        adapter = core.adapters.get(p.id)
        provider_rows.append(
            {
                "id": p.id,
                "enabled": p.id in enabled,
                "configured": bool(adapter and adapter.is_configured()),
                "env_var": adapter.env_var if adapter else None,
                "models": sum(1 for m in active if m.service_provider_id == p.id),
                "probe": cached_probe(p.id),
            }
        )

    # Each line is a thing to go and fix, and each one is a link on screen.
    work: list[dict[str, Any]] = []
    if unpriced:
        work.append({
            "text": f"{len(unpriced)} моделей без цены — расход по ним считать нечем",
            "href": "/models",
        })
    if unverified:
        work.append({
            "text": f"{len(unverified)} моделей с неподтверждённой ценой",
            "href": "/models",
        })
    if day_totals["unknown_requests"]:
        work.append({
            "text": f"{day_totals['unknown_requests']} запросов с неизвестной стоимостью",
            "href": "/logs?unverified_cost=true",
        })
    # Our arithmetic against the provider's charge, over the last week. More
    # than 2% and more than a tenth of a cent apart means the row's rate is
    # missing something — both past finds (the search fee, the cache rates)
    # looked exactly like this.
    mismatched = await core.db.count_request_logs(
        cost_mismatch=True, since=_hours_ago(7 * 24)
    )
    if mismatched:
        work.append({
            "text": f"{mismatched} запросов за неделю разошлись с суммой поставщика",
            "href": "/logs?cost_mismatch=true",
        })
    broken = [p for p in provider_rows if p["enabled"] and not p["configured"]]
    for p in broken:
        work.append({
            "text": f"{p['id']} включён, но не настроен"
                    + (f" — переменная {p['env_var']} не задана" if p["env_var"] else ""),
            "href": "/connections",
        })
    # A key that is present and a provider that still cannot be sent a photograph
    # are different states, and only the first one had a line here. Without the
    # public-link settings, every image request from a phone fails at the adapter
    # — and only on the first real one, after the key, the model and the money.
    for warning in public_blobs.installation_warnings(core.settings, core.adapters):
        work.append({"text": warning, "href": "/connections"})
    if blob_bytes > 500 * 1024 * 1024:
        work.append({
            "text": f"файлы ответов занимают {blob_bytes // (1024 * 1024)} МБ, "
                    "политика хранения не настроена",
            "href": "/logs",
        })

    return {
        "version": __version__,
        "uptime_seconds": int(time.time() - started_at),
        "db_bytes": db_bytes,
        "blob_bytes": blob_bytes,
        "providers": provider_rows,
        "providers_usable": sum(1 for p in provider_rows if p["enabled"] and p["configured"]),
        "providers_total": len(provider_rows),
        "models_active": len(active),
        "models_retired": len(models) - len(active),
        "models_unpriced": len(unpriced),
        "models_unverified": len(unverified),
        "catalog_last_seen_at": freshness or None,
        "last_24h": day_totals,
        "work": work,
    }


def _hours_ago(hours: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
