"""Editing a price in the registry.

Two defects this pins down (§11 of docs/UI_CONCEPT_v2.md):

* the only way to edit a row used to be a whole-row write, which filled
  everything the form did not carry with defaults — context length and catalog
  dates were wiped and a retired model came back as active;
* price protection was keyed on the origin of the *row*, so a price typed into a
  catalog row was overwritten by the next synchronisation. It is now keyed on
  the origin of the *price*: the row stays a catalog row and keeps receiving
  name and context updates, while the number a human vouched for survives.
"""

from __future__ import annotations

import pytest

from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.storage.database import Database


def _catalog_row(**kw) -> RegistryModel:
    defaults = dict(
        service_provider_id="openrouter",
        model_provider="openai",
        model_id="openai/gpt-4o-mini",
        display_name="GPT-4o mini",
        modalities=(Modality.CHAT,),
        prompt_price=0.15e-6,
        completion_price=0.6e-6,
        context_length=128000,
        source="catalog",
        price_source="api:/models",
        price_source_kind="api",
        last_seen_at="2026-07-20T00:00:00+00:00",
    )
    defaults.update(kw)
    return RegistryModel(**defaults)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "r.db"))
    await database.init()
    await database.upsert_service_provider(
        "openrouter", "OpenRouter", "openrouter", None, enabled=True
    )
    await database.upsert_registry_model(_catalog_row())
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_partial_edit_keeps_everything_it_was_not_given(db):
    await db.update_registry_model(
        "openrouter", "openai/gpt-4o-mini", {"prompt_price": 0.2e-6}
    )
    row = await db.get_registry_model("openrouter", "openai/gpt-4o-mini")
    assert row.prompt_price == 0.2e-6
    # Untouched by an edit that only mentioned the price.
    assert row.context_length == 128000
    assert row.last_seen_at == "2026-07-20T00:00:00+00:00"
    assert row.source == "catalog"
    assert row.is_active is True


@pytest.mark.asyncio
async def test_an_edit_cannot_resurrect_a_retired_model(db):
    await db.deprecate_registry_model("openrouter", "openai/gpt-4o-mini")
    await db.update_registry_model(
        "openrouter", "openai/gpt-4o-mini", {"prompt_price": 0.2e-6}
    )
    row = await db.get_registry_model("openrouter", "openai/gpt-4o-mini")
    assert row.is_active is False
    assert row.deprecated_at


@pytest.mark.asyncio
async def test_optimistic_lock_refuses_a_stale_write(db):
    row = await db.get_registry_model("openrouter", "openai/gpt-4o-mini")
    # Somebody else writes first.
    await db.update_registry_model(
        "openrouter", "openai/gpt-4o-mini", {"display_name": "Renamed"}
    )
    lost = await db.update_registry_model(
        "openrouter", "openai/gpt-4o-mini",
        {"prompt_price": 9e-6},
        if_revision=row.revision,
    )
    assert lost is None
    fresh = await db.get_registry_model("openrouter", "openai/gpt-4o-mini")
    assert fresh.prompt_price == 0.15e-6


@pytest.mark.asyncio
async def test_patchable_columns_are_a_closed_set(db):
    with pytest.raises(ValueError):
        await db.update_registry_model(
            "openrouter", "openai/gpt-4o-mini", {"is_active": 0}
        )


@pytest.mark.asyncio
async def test_a_hand_entered_price_survives_the_next_sync(db):
    """The whole point: correcting a price in a catalog row must stick."""
    await db.update_registry_model(
        "openrouter", "openai/gpt-4o-mini",
        {"prompt_price": 0.99e-6, "price_source_kind": "manual"},
    )
    # The catalog comes back with its own (different) price.
    merged = RegistryService._merge(
        await db.get_registry_model("openrouter", "openai/gpt-4o-mini"),
        _catalog_row(prompt_price=0.15e-6, display_name="GPT-4o mini (2026)"),
        "2026-07-26T00:00:00+00:00",
    )
    assert merged.prompt_price == 0.99e-6          # the human's number stands
    assert merged.display_name == "GPT-4o mini (2026)"  # the rest still updates
    assert merged.source == "catalog"
    assert merged.last_seen_at == "2026-07-26T00:00:00+00:00"


@pytest.mark.asyncio
async def test_revert_hands_the_row_back_to_the_catalog(db):
    await db.update_registry_model(
        "openrouter", "openai/gpt-4o-mini",
        {"prompt_price": 0.99e-6, "price_source_kind": "manual"},
    )
    await db.update_registry_model(
        "openrouter", "openai/gpt-4o-mini",
        {"price_source_kind": "api", "price_source": None, "price_checked_at": None},
    )
    merged = RegistryService._merge(
        await db.get_registry_model("openrouter", "openai/gpt-4o-mini"),
        _catalog_row(prompt_price=0.15e-6),
        "2026-07-26T00:00:00+00:00",
    )
    assert merged.prompt_price == 0.15e-6


def test_api_patch_round_trip(client):
    """Through HTTP: prices go in as $/1M and the answer carries a fresh lock."""
    created = client.post(
        "/api/v1/registry/models",
        json={
            "service_provider_id": "openrouter",
            "model_id": "demo/patchable",
            "modalities": ["chat"],
            "prompt_price": 1e-6,
            "context_length": 64000,
        },
    )
    assert created.status_code == 200, created.text

    patched = client.patch(
        "/api/v1/registry/models/openrouter/demo/patchable",
        json={"prompt_price": 2.5, "completion_price": 7.0},
    ).json()
    assert patched["prompt_price"] == pytest.approx(2.5e-6)
    assert patched["completion_price"] == pytest.approx(7.0e-6)
    assert patched["price_source_kind"] == "manual"
    assert patched["context_length"] == 64000
    assert patched["revision"] >= 1

    stale = client.patch(
        "/api/v1/registry/models/openrouter/demo/patchable",
        json={"prompt_price": 3.0, "revision": 0},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "conflict"


def test_api_patch_unknown_pair_is_404(client):
    r = client.patch("/api/v1/registry/models/openrouter/nope/nope", json={"tier": "cheap"})
    assert r.status_code == 404


def test_cache_rates_are_reachable_through_the_api(client):
    """Without this field accounting charges cache reads at the prompt rate and
    marks the whole figure estimated — a several-fold overstatement that has to
    be fixable from the console, not only from SQL."""
    client.post(
        "/api/v1/registry/models",
        json={
            "service_provider_id": "deepseek",
            "model_id": "demo/cached",
            "modalities": ["chat"],
        },
    )
    patched = client.patch(
        "/api/v1/registry/models/deepseek/demo/cached",
        json={"prompt_price": 0.435, "completion_price": 0.87,
              "cache_read_price": 0.003625},
    ).json()
    assert patched["prompt_price"] == pytest.approx(0.435e-6)
    assert patched["cache_read_price"] == pytest.approx(0.003625e-6)
    # Touching a cache rate is touching a price, so provenance follows.
    assert patched["price_source_kind"] == "manual"
