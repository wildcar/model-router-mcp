"""Catalog reconciliation: what a provider's model list may and may not change.

The scenario behind these tests is real: DeepSeek retired `deepseek-chat` and
`deepseek-reasoner` in favour of `deepseek-v4-pro`, the registry kept serving the
dead ids, and every restart re-seeded them.
"""

from __future__ import annotations

import pytest

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.storage.database import Database, RequestLogRecord


def _model(model_id: str, **kw) -> RegistryModel:
    base = dict(
        service_provider_id="vendor",
        model_provider="vendor",
        model_id=model_id,
        display_name=model_id,
        modalities=(Modality.CHAT,),
        source="catalog",
    )
    base.update(kw)
    return RegistryModel(**base)


class FakeCatalogAdapter(ProviderAdapter):
    """A provider whose catalog the test controls."""

    name = "vendor"
    supported_tasks = {Task.TEXT_TO_TEXT}

    def __init__(self, catalog: list[RegistryModel] | None) -> None:
        self.catalog = catalog

    def is_configured(self) -> bool:
        return True

    async def list_catalog(self):
        return self.catalog

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        return RouterResponse(
            task=Task.TEXT_TO_TEXT, provider=self.name, model_id=model.model_id, usage=Usage()
        )


async def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "sync.db"))
    await db.init()
    await db.upsert_service_provider("vendor", "Vendor", "vendor", None, enabled=True)
    return db


@pytest.mark.asyncio
async def test_sync_adds_new_and_deprecates_retired(tmp_path):
    db = await _db(tmp_path)
    try:
        adapter = FakeCatalogAdapter([_model("v3-chat")])
        registry = RegistryService(db, adapters={"vendor": adapter})
        await registry.sync_all(force=True)

        adapter.catalog = [_model("v4-pro")]
        report = (await registry.sync_all(force=True))[0]

        assert (report.added, report.deprecated) == (1, 1)
        active = {m.model_id for m in await db.list_registry_models()}
        assert active == {"v4-pro"}

        # The retired row survives — request logs and costs still point at it.
        retired = [
            m
            for m in await db.list_registry_models(active_only=False)
            if m.model_id == "v3-chat"
        ][0]
        assert retired.is_active is False
        assert retired.deprecated_at is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_priceless_catalog_keeps_manual_prices(tmp_path):
    """DeepSeek/Anthropic list ids without prices; a sync must not zero the
    number an admin entered by hand."""
    db = await _db(tmp_path)
    try:
        await db.upsert_registry_model(
            _model(
                "v4-pro",
                display_name="DeepSeek v4 Pro (checked)",
                source="manual",
                prompt_price=2.7e-7,
                completion_price=1.1e-6,
                price_source_kind="manual",
            )
        )
        registry = RegistryService(
            db,
            adapters={"vendor": FakeCatalogAdapter([_model("v4-pro", price_source_kind="unknown")])},
        )
        await registry.sync_all(force=True)

        stored = (await db.list_registry_models())[0]
        assert stored.prompt_price == pytest.approx(2.7e-7)
        assert stored.completion_price == pytest.approx(1.1e-6)
        assert stored.display_name == "DeepSeek v4 Pro (checked)"
        assert stored.source == "manual"
        assert stored.last_seen_at is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_priced_catalog_updates_catalog_rows(tmp_path):
    db = await _db(tmp_path)
    try:
        adapter = FakeCatalogAdapter(
            [_model("m1", prompt_price=1e-6, price_source_kind="api", price_source="api:x")]
        )
        registry = RegistryService(db, adapters={"vendor": adapter})
        await registry.sync_all(force=True)

        adapter.catalog = [
            _model("m1", prompt_price=5e-7, price_source_kind="api", price_source="api:x")
        ]
        await registry.sync_all(force=True)

        stored = (await db.list_registry_models())[0]
        assert stored.prompt_price == pytest.approx(5e-7)
        assert stored.price_source_kind == "api"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_seed_never_resurrects_a_retired_model(tmp_path):
    db = await _db(tmp_path)
    try:
        seed = _model("v3-chat", source="seed", prompt_price=1e-6)
        assert await db.seed_registry_model(seed) is True
        await db.deprecate_registry_model("vendor", "v3-chat")

        # Restart: bootstrap re-seeds the same row.
        assert await db.seed_registry_model(seed) is False

        rows = await db.list_registry_models(active_only=False)
        assert [(m.model_id, m.is_active) for m in rows] == [("v3-chat", False)]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_provider_without_catalog_is_left_alone(tmp_path):
    """`None` means "cannot ask" (CLI engines): the registry must not be wiped."""
    db = await _db(tmp_path)
    try:
        await db.upsert_registry_model(_model("cli", source="seed"))
        registry = RegistryService(db, adapters={"vendor": FakeCatalogAdapter(None)})
        report = (await registry.sync_all(force=True))[0]

        assert report.skipped == "no catalog endpoint"
        assert [m.model_id for m in await db.list_registry_models()] == ["cli"]
    finally:
        await db.close()


# --- browsed catalogs (ADR-0008) ---------------------------------------
class FakeBrowseAdapter(FakeCatalogAdapter):
    """A provider whose catalog is a market: asked, not mirrored."""

    catalog_scope = "browse"


@pytest.mark.asyncio
async def test_browsed_catalog_refreshes_known_rows_and_inserts_none(tmp_path):
    """OpenRouter offers four hundred ids; five were ever addressed here. The
    sync must keep those five fresh and leave the rest out of the registry —
    every mirrored row joined the pool automatic selection picks from."""
    db = await _db(tmp_path)
    try:
        await db.upsert_registry_model(_model("known", prompt_price=1e-6))
        adapter = FakeBrowseAdapter([
            _model("known", prompt_price=2e-6, price_source_kind="api"),
            _model("offered-1"),
            _model("offered-2"),
        ])
        report = (await RegistryService(db, adapters={"vendor": adapter}).sync_all(force=True))[0]

        assert (report.added, report.updated, report.offered) == (0, 1, 2)
        stored = await db.list_registry_models(active_only=False)
        assert [m.model_id for m in stored] == ["known"]
        assert stored[0].prompt_price == pytest.approx(2e-6)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_browse_reports_what_is_known_and_confirmed(tmp_path):
    db = await _db(tmp_path)
    try:
        await db.upsert_registry_model(_model("known"))
        await db.confirm_registry_model("vendor", "known", "2026-07-30T10:00:00+00:00")
        adapter = FakeBrowseAdapter([_model("known"), _model("offered")])
        answer = await RegistryService(db, adapters={"vendor": adapter}).browse_provider("vendor")

        assert answer["total"] == 2 and answer["known"] == 1
        by_id = {i["model_id"]: i for i in answer["items"]}
        assert by_id["known"]["confirmed_at"] == "2026-07-30T10:00:00+00:00"
        assert by_id["offered"]["known"] is False
        # Browsing stores nothing: the second model is still not a row.
        assert [m.model_id for m in await db.list_registry_models()] == ["known"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_adopt_writes_the_providers_row_not_the_browsers(tmp_path):
    db = await _db(tmp_path)
    try:
        adapter = FakeBrowseAdapter([_model("offered", prompt_price=3e-6, display_name="Offered")])
        registry = RegistryService(db, adapters={"vendor": adapter})
        assert await registry.adopt_models("vendor", ["offered"]) == ["offered"]

        stored = (await db.list_registry_models())[0]
        assert stored.display_name == "Offered"
        assert stored.prompt_price == pytest.approx(3e-6)
        # Adopted, not confirmed: only an answer confirms an id.
        assert stored.confirmed_at is None

        with pytest.raises(Exception):
            await registry.adopt_models("vendor", ["never-offered"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prune_keeps_confirmed_curated_and_logged_rows(tmp_path):
    db = await _db(tmp_path)
    try:
        await db.upsert_registry_model(_model("answered"))
        await db.confirm_registry_model("vendor", "answered")
        await db.upsert_registry_model(_model("by-hand", source="manual"))
        await db.upsert_registry_model(_model("priced-by-hand", price_source_kind="page"))
        await db.upsert_registry_model(_model("seeded", source="seed"))
        await db.upsert_registry_model(_model("tried-and-failed"))
        await db.upsert_registry_model(_model("never-touched"))
        await db.log_request(
            RequestLogRecord(
                external_user_id="u",
                modality="chat",
                service_provider_id="vendor",
                model_id="tried-and-failed",
                status="error",
            )
        )
        registry = RegistryService(db, adapters={"vendor": FakeBrowseAdapter([])})

        plan = await registry.prune_unconfirmed("vendor", dry_run=True)
        assert plan.retired == ["never-touched"]
        assert (plan.kept_confirmed, plan.kept_curated, plan.kept_in_log) == (1, 3, 1)
        # A dry run changes nothing.
        assert len(await db.list_registry_models()) == 6

        done = await registry.prune_unconfirmed("vendor", dry_run=False)
        assert done.retired == ["never-touched"]
        active = {m.model_id for m in await db.list_registry_models()}
        assert "never-touched" not in active and len(active) == 5
        # Retired, not deleted.
        assert len(await db.list_registry_models(active_only=False)) == 6
    finally:
        await db.close()
