"""RegistryService — the model catalog and its reconciliation with providers.

Every adapter that can answer "which models do you have right now" implements
`list_catalog()`; this service asks each of them on a TTL (and on a background
timer), then reconciles the answer with `registry_models`:

  * seen upstream        -> refreshed, `last_seen_at` bumped, re-activated;
  * missing upstream     -> `is_active = 0` + `deprecated_at` (never deleted,
                            request logs and cost history point at the row);
  * manual rows          -> keep their prices, name and pricing mode, and are
                            never deprecated by a sync: for an allowlist-shaped
                            catalog (Replicate's `REPLICATE_MODELS`) "not listed"
                            only means "not allowlisted", and curation is
                            withdrawn by hand, not by a sweep;
  * catalogs without prices (DeepSeek, Anthropic) never zero an existing price;
  * browsed catalogs (`catalog_scope = "browse"`, i.e. OpenRouter) refresh the
    rows we already hold and insert none: a market of four hundred ids is asked
    on demand (`browse_provider`) and a row appears when an operator adopts a
    model or a request to it comes back with an answer (ADR-0008).

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
from router_mcp.domain.errors import BadRequest, NotConfigured
from router_mcp.registry.models import RegistryModel
from router_mcp.storage.database import Database

logger = logging.getLogger(__name__)

OPENROUTER_PROVIDER_ID = "openrouter"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# How long a row the *backend* retired stays retired while the catalog keeps
# listing it. gpt-5.5 (2026-09-08) was listed by `GET /models` and refused by
# `/responses` with 404 model_not_found; a sync every 30 minutes would have
# revived the row and the next request would have retired it again — one wasted
# call per half hour, and the automatic pick landing on a dead id in between.
# After the hold the catalog is believed again: if the backend still refuses,
# the first request retires the row for another day (ADR-0010).
BACKEND_RETIREMENT_HOLD_SECONDS = 24 * 3600


def _held_by_backend(model: RegistryModel, now: datetime) -> bool:
    """True while a backend-retired row must not be revived by the catalog."""
    if model.is_active or model.retired_by != "backend" or not model.deprecated_at:
        return False
    try:
        since = datetime.fromisoformat(model.deprecated_at)
    except ValueError:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return (now - since).total_seconds() < BACKEND_RETIREMENT_HOLD_SECONDS


@dataclass(slots=True)
class SyncReport:
    """What one provider sync did, for the API and the logs."""

    provider_id: str
    seen: int = 0
    added: int = 0
    updated: int = 0
    deprecated: int = 0
    # Models the provider offers that the registry deliberately did not store,
    # because this catalog is browsed rather than mirrored. Reported so the
    # console can say «предлагает 424, храним 6» instead of looking broken.
    offered: int = 0
    # Rows the catalog lists that stayed retired anyway, because a request on
    # them was refused by the backend less than a day ago (ADR-0010).
    held: int = 0
    skipped: str | None = None  # why nothing happened, when nothing happened

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class PruneReport:
    """What a sweep of unconfirmed rows retired or deleted, or would."""

    provider_id: str
    dry_run: bool
    retired: list[str] = dataclasses.field(default_factory=list)
    # Rows that were already retired and that nothing confirmed, nobody curated
    # and no log entry names: deleted outright, because a row nothing points at
    # is not history — it is clutter in every list with a «снятые» filter.
    deleted: list[str] = dataclasses.field(default_factory=list)
    kept_confirmed: int = 0
    kept_curated: int = 0
    kept_in_log: int = 0

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
        now = datetime.now(timezone.utc)
        browse_only = getattr(adapter, "catalog_scope", "full") == "browse"
        seen: set[str] = set()
        for fresh in incoming:
            seen.add(fresh.model_id)
            old = existing.get(fresh.model_id)
            if old is not None and _held_by_backend(old, now):
                # The catalog says the id exists; the backend said otherwise to
                # a real request, and more recently. The catalog is the weaker
                # witness here (ADR-0008 says a catalog is a question), so the
                # row stays retired until the hold runs out.
                report.held += 1
                continue
            if browse_only and (old is None or not old.is_active):
                # A browsed catalog is not an inventory: mirroring it filled the
                # registry with hundreds of ids nobody addressed, and every one of
                # them joined the automatic-selection pool. The list stays
                # available through `browse_provider`.
                #
                # Retired rows are skipped too, and that is the difference between
                # the scopes in one line. A full catalog re-activates a row the
                # provider lists again — the registry follows the provider. In a
                # browse catalog the provider lists everything always, so "listed
                # upstream" says nothing; what retired the row was the operator's
                # sweep, and the first sync after it must not quietly undo it.
                # The way back is `adopt_models`, which is explicit.
                report.offered += 1
                continue
            await self._db.upsert_registry_model(self._merge(old, fresh, stamp))
            if old is None:
                report.added += 1
            else:
                report.updated += 1
        report.seen = len(seen)

        for model_id, old in existing.items():
            if model_id in seen or not old.is_active:
                continue
            if old.source == "manual":
                # An operator's row, not the catalog's. "Missing upstream" is
                # only meaningful for a catalog that lists everything the
                # provider serves — Replicate's is the `REPLICATE_MODELS`
                # allowlist, so a manual row would be retired six hours after
                # it was added. Curation ends by hand (deprecate/delete), the
                # same rule `prune_unconfirmed` already applies.
                continue
            await self._db.deprecate_registry_model(provider_id, model_id)
            report.deprecated += 1
        return report

    # --- browse / adopt / prune ------------------------------------------
    async def browse_provider(self, provider_id: str) -> dict:
        """Ask one provider what it offers right now, and store nothing.

        This is the answer to «какие модели вообще есть» — the question the
        registry could not answer for a provider whose catalog it does not mirror,
        and answered wrongly for one whose seed row was written by hand once
        (codex-oauth looked like it had a single text model; it has six).

        Every entry says whether the registry already holds it and whether a
        request to it ever came back with an answer, because those are the two
        facts that decide whether it is worth adopting.
        """
        adapter = self._require_adapter(provider_id)
        if not adapter.is_configured():
            raise NotConfigured(
                f"Provider {provider_id!r} has no credentials, so it cannot be asked"
            )
        incoming = await adapter.list_catalog()
        if incoming is None:
            return {
                "provider_id": provider_id,
                "scope": getattr(adapter, "catalog_scope", "full"),
                "has_catalog": False,
                "total": 0,
                "known": 0,
                "items": [],
            }
        existing = {
            m.model_id: m
            for m in await self._db.list_registry_models(
                service_provider_id=provider_id, active_only=False
            )
        }
        items = []
        for fresh in incoming:
            old = existing.get(fresh.model_id)
            items.append(
                {
                    "model_id": fresh.model_id,
                    "display_name": fresh.display_name,
                    "tasks": [t.value for t in fresh.tasks],
                    "capabilities": list(fresh.capabilities),
                    "context_length": fresh.context_length,
                    "prompt_price": fresh.prompt_price,
                    "completion_price": fresh.completion_price,
                    "price_source_kind": fresh.price_source_kind,
                    "known": old is not None,
                    "active": bool(old and old.is_active),
                    "confirmed_at": old.confirmed_at if old else None,
                }
            )
        items.sort(key=lambda i: (not i["known"], i["model_id"]))
        return {
            "provider_id": provider_id,
            "scope": getattr(adapter, "catalog_scope", "full"),
            "has_catalog": True,
            "total": len(items),
            "known": sum(1 for i in items if i["known"]),
            "items": items,
        }

    async def adopt_models(self, provider_id: str, model_ids: list[str]) -> list[str]:
        """Put named models from the live catalog into the registry.

        The row is written from the provider's own answer rather than from what
        the browser displayed: a form can be stale, and a price typed by a page
        is not a price the provider published. An adopted row starts unconfirmed
        — it becomes permanent when a request to it succeeds.
        """
        adapter = self._require_adapter(provider_id)
        incoming = await adapter.list_catalog()
        if incoming is None:
            raise BadRequest(f"Provider {provider_id!r} has no catalog to adopt from")
        wanted = set(model_ids)
        offered = {m.model_id: m for m in incoming}
        missing = sorted(wanted - set(offered))
        if missing:
            raise BadRequest(
                f"{provider_id} does not offer: {', '.join(missing)}"
            )
        stamp = _now()
        adopted: list[str] = []
        async with self._lock:
            for model_id in sorted(wanted):
                old = await self._db.get_registry_model(provider_id, model_id)
                await self._db.upsert_registry_model(
                    self._merge(old, offered[model_id], stamp)
                )
                adopted.append(model_id)
        return adopted

    async def prune_unconfirmed(
        self, provider_id: str, *, dry_run: bool = True
    ) -> PruneReport:
        """Retire the rows nothing ever confirmed, keeping what was curated.

        Four reasons a row survives: a request to it came back (`confirmed_at`),
        an admin created or priced it by hand, a bootstrap seed put it there, or
        the log mentions it at all — a failed attempt is still the row that
        explains the error somebody is reading.

        Active unconfirmed rows are retired, not deleted: they stay in the
        table, out of the active pool, and «восстановить» is one call away.
        Rows that are *already* retired and that none of the four reasons
        protects are deleted outright — «не удаляем, на строку ссылаются»
        is vacuous for a row nothing ever referenced, and such rows only
        clutter every list that can show «снятые».
        """
        self._require_adapter(provider_id)
        report = PruneReport(provider_id=provider_id, dry_run=dry_run)
        in_log = await self._db.model_ids_in_logs(provider_id)
        for model in await self._db.list_registry_models(
            service_provider_id=provider_id, active_only=False
        ):
            curated = model.source in ("manual", "seed") or model.price_source_kind in (
                "manual",
                "page",
            )
            if not model.is_active:
                if not model.confirmed_at and not curated and model.model_id not in in_log:
                    report.deleted.append(model.model_id)
                continue
            if model.confirmed_at:
                report.kept_confirmed += 1
            elif curated:
                report.kept_curated += 1
            elif model.model_id in in_log:
                report.kept_in_log += 1
            else:
                report.retired.append(model.model_id)
        if not dry_run:
            for model_id in report.retired:
                await self._db.deprecate_registry_model(provider_id, model_id)
            for model_id in report.deleted:
                await self._db.delete_registry_model(provider_id, model_id)
        return report

    def _require_adapter(self, provider_id: str) -> ProviderAdapter:
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise BadRequest(f"Unknown provider {provider_id!r}")
        return adapter

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
        # and task updates while the number a human vouched for survives.
        human_priced = old.price_source_kind in ("manual", "page")
        prices_authoritative = fresh.price_source_kind == "api" and not human_priced
        if prices_authoritative:
            prices = {
                "prompt_price": fresh.prompt_price,
                "completion_price": fresh.completion_price,
                "image_price": fresh.image_price,
                "audio_price_per_second": fresh.audio_price_per_second,
                # These three were missing, so a catalog that publishes them would
                # not have been believed. Not a live bug today (no catalog here is
                # `api`-priced beyond OpenRouter, which publishes none of them),
                # but the next one would have inherited the gap.
                "character_price": fresh.character_price,
                "cache_read_price": fresh.cache_read_price,
                "cache_write_price": fresh.cache_write_price,
                "web_search_price": fresh.web_search_price,
                "compute_price_per_second": fresh.compute_price_per_second,
                "tier": fresh.tier,
                "price_source": fresh.price_source,
                "price_source_kind": fresh.price_source_kind,
                "price_checked_at": fresh.price_checked_at or stamp,
            }
            # `price_schedule` is not in here, and cannot be: the sync does not
            # write that column at all. It does not have to be — typing a
            # schedule counts as a human price (`api_patch_model`), so a row that
            # has one is never `prices_authoritative` in the first place.
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
            # A catalog that now reports tasks correctly overwrites what the
            # migration derived from the coarse group, which is how the OpenRouter
            # rows heal themselves on the next sync.
            tasks=fresh.tasks or old.tasks,
            modalities=fresh.modalities or old.modalities,
            capabilities=fresh.capabilities or old.capabilities,
            context_length=fresh.context_length or old.context_length,
            model_provider=fresh.model_provider or old.model_provider,
            source="catalog" if old.source in ("catalog", "seed") else old.source,
            is_active=True,
            last_seen_at=stamp,
            deprecated_at=None,
            # A revived row is the catalog's again; whoever retired it before
            # is no longer the reason it is in this state.
            retired_by=None,
            **prices,
        )

    # --- retirement by a refusal -------------------------------------------
    async def retire_model(
        self,
        provider_id: str,
        model_id: str,
        *,
        reason: str,
        replaced_by: str | None = None,
    ) -> None:
        """Retire a row because the provider refused a request on it.

        The catalog is one witness to a model's existence and an answer is the
        proof (ADR-0008); a definitive refusal — 404 model_not_found, 400 «model
        is not supported» — is the proof of the opposite, and waiting for the
        catalog to agree left gpt-5.5 addressable for a day after it was gone.
        The row is retired at once, marked `retired_by="backend"` so the next
        sync does not revive it on the catalog's word alone, and `replaced_by`
        names what answered instead when the caller knows.
        """
        logger.warning(
            "Retiring %s/%s on the backend's refusal: %s", provider_id, model_id, reason
        )
        await self._db.deprecate_registry_model(
            provider_id, model_id, replaced_by=replaced_by, retired_by="backend"
        )

    # --- reads ------------------------------------------------------------
    async def get_models(
        self,
        *,
        modality: str | None = None,
        task: str | None = None,
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
            task=task,
            tier=tier,
            service_provider_id=provider,
            active_only=active_only,
        )
