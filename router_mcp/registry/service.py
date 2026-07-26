"""RegistryService — the model catalog and its reconciliation with providers.

Every adapter that can answer "which models do you have right now" implements
`list_catalog()`; this service asks each of them on a TTL (and on a background
timer), then reconciles the answer with `registry_models`:

  * seen upstream        -> refreshed, `last_seen_at` bumped, re-activated;
  * missing upstream     -> `is_active = 0` + `deprecated_at` (never deleted,
                            request logs and cost history point at the row);
  * manual rows          -> keep their prices, name and pricing mode; the sync
                            only updates freshness and availability;
  * catalogs without prices (DeepSeek, Anthropic) never zero an existing price.

All reads go through the DB so routing, the MCP tools and the admin UI see one
merged view.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.domain.errors import BadRequest, RouterError
from router_mcp.registry.models import RegistryModel
from router_mcp.storage.database import Database

logger = logging.getLogger(__name__)

OPENROUTER_PROVIDER_ID = "openrouter"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class SyncReport:
    """What one provider sync did, for the API and the logs."""

    provider_id: str
    seen: int = 0
    added: int = 0
    updated: int = 0
    deprecated: int = 0
    skipped: str | None = None  # why nothing happened, when nothing happened

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class RegistryService:
    def __init__(
        self,
        db: Database,
        adapters: Mapping[str, ProviderAdapter] | None = None,
        ttl_seconds: int = 1800,
    ) -> None:
        self._db = db
        self._adapters = dict(adapters or {})
        self._ttl_seconds = ttl_seconds
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:  # pragma: no cover - adapters own their clients
        return None

    # --- sync -------------------------------------------------------------
    async def sync_all(self, force: bool = False) -> list[SyncReport]:
        """Reconcile every provider that has a catalog. TTL-guarded unless forced."""
        now = time.monotonic()
        if not force and now < self._expires_at:
            return []
        async with self._lock:
            if not force and time.monotonic() < self._expires_at:
                return []
            reports: list[SyncReport] = []
            for provider_id in sorted(self._adapters):
                reports.append(await self._sync_one(provider_id))
            # The TTL is set even when individual providers failed: a broken
            # upstream must not turn every request into a retry storm.
            self._expires_at = time.monotonic() + self._ttl_seconds
            synced = [r for r in reports if r.skipped is None]
            if synced:
                logger.info(
                    "Catalog sync: %s",
                    ", ".join(
                        f"{r.provider_id} seen={r.seen} +{r.added} ~{r.updated} -{r.deprecated}"
                        for r in synced
                    ),
                )
            return reports

    async def sync_provider(self, provider_id: str) -> SyncReport:
        """Reconcile a single provider now (ignores the TTL)."""
        if provider_id not in self._adapters:
            raise BadRequest(f"Unknown provider {provider_id!r}")
        async with self._lock:
            return await self._sync_one(provider_id)

    async def _sync_one(self, provider_id: str) -> SyncReport:
        adapter = self._adapters[provider_id]
        report = SyncReport(provider_id=provider_id)
        if not adapter.is_configured():
            report.skipped = "not configured"
            return report
        try:
            incoming = await adapter.list_catalog()
        except Exception as exc:  # a dead provider must not break the others
            logger.warning("Catalog fetch failed for %s: %s", provider_id, exc)
            report.skipped = f"fetch failed: {exc}"
            return report
        if incoming is None:
            report.skipped = "no catalog endpoint"
            return report

        existing = {
            m.model_id: m
            for m in await self._db.list_registry_models(
                service_provider_id=provider_id, active_only=False
            )
        }
        stamp = _now()
        seen: set[str] = set()
        for fresh in incoming:
            seen.add(fresh.model_id)
            old = existing.get(fresh.model_id)
            await self._db.upsert_registry_model(self._merge(old, fresh, stamp))
            if old is None:
                report.added += 1
            else:
                report.updated += 1
        report.seen = len(seen)

        for model_id, old in existing.items():
            if model_id in seen or not old.is_active:
                continue
            await self._db.deprecate_registry_model(provider_id, model_id)
            report.deprecated += 1
        return report

    @staticmethod
    def _merge(old: RegistryModel | None, fresh: RegistryModel, stamp: str) -> RegistryModel:
        """Fold a catalog row into the stored one without losing curated data."""
        if old is None:
            return dataclasses.replace(fresh, last_seen_at=stamp, deprecated_at=None)

        # Only a catalog that publishes prices may rewrite them, and only where
        # the stored price is not one a human put there.
        #
        # The criterion is the origin of the *price*, not the origin of the
        # *row*. Keyed on the row, a price typed into a catalog row was wiped by
        # the very next sync — which is where corrected prices went. Keyed on the
        # price, the row stays a catalog row and keeps receiving name, context
        # and modality updates while the number a human vouched for survives.
        human_priced = old.price_source_kind in ("manual", "page")
        prices_authoritative = fresh.price_source_kind == "api" and not human_priced
        if prices_authoritative:
            prices = {
                "prompt_price": fresh.prompt_price,
                "completion_price": fresh.completion_price,
                "image_price": fresh.image_price,
                "audio_price_per_second": fresh.audio_price_per_second,
                "tier": fresh.tier,
                "price_source": fresh.price_source,
                "price_source_kind": fresh.price_source_kind,
                "price_checked_at": fresh.price_checked_at or stamp,
            }
        else:
            # Keep the price, but record the page the provider publishes it on
            # when we have nothing better on file.
            prices = {
                "price_source": old.price_source or fresh.price_source,
                "price_source_kind": old.price_source_kind,
            }

        return dataclasses.replace(
            old,
            display_name=old.display_name if old.source == "manual" else fresh.display_name,
            modalities=fresh.modalities or old.modalities,
            capabilities=fresh.capabilities or old.capabilities,
            context_length=fresh.context_length or old.context_length,
            model_provider=fresh.model_provider or old.model_provider,
            source="catalog" if old.source in ("catalog", "seed") else old.source,
            is_active=True,
            last_seen_at=stamp,
            deprecated_at=None,
            **prices,
        )

    # --- reads ------------------------------------------------------------
    async def get_models(
        self,
        *,
        modality: str | None = None,
        tier: str | None = None,
        provider: str | None = None,
        force_refresh: bool = False,
        active_only: bool = True,
        allow_sync: bool = True,
    ) -> list[RegistryModel]:
        """Registry rows, refreshing from the providers first if the TTL says so.

        `allow_sync=False` is for the dry run: a plan that reaches out to every
        provider's catalog is neither dry nor fast, and "показать план" promises
        that nothing leaves the host.
        """
        if allow_sync:
            await self.sync_all(force=force_refresh)
        return await self._db.list_registry_models(
            modality=modality,
            tier=tier,
            service_provider_id=provider,
            active_only=active_only,
        )
