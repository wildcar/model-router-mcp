"""The dry run: what would happen, with nothing happening.

Two promises the button makes — "ничего не отправляем и не тратим", and an
answer even when nothing can run. The first is broken by a plan that quietly
polls eight provider catalogs on the way; the second by returning an error
status, which leaves the "почему запрос не ушёл" screen with nothing to draw.
"""

from __future__ import annotations

import pytest

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.messages import FallbackPolicy, RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database


class CountingAdapter(ProviderAdapter):
    """Configured, and loudly unhappy if anyone tries to use it."""

    supported_tasks = {Task.TEXT_TO_TEXT}
    SUPPORTED_PARAMS = {Task.TEXT_TO_TEXT: ("temperature",)}

    def __init__(self, name: str) -> None:
        self.name = name
        self.catalog_calls = 0
        self.generate_calls = 0

    def is_configured(self) -> bool:
        return True

    async def list_catalog(self):
        self.catalog_calls += 1
        return []

    async def generate(self, request, model):
        self.generate_calls += 1
        raise AssertionError("the plan must not call a provider")


def _model(provider: str, model_id: str, price: float, **kw) -> RegistryModel:
    return RegistryModel(
        provider, provider, model_id, model_id, (Modality.CHAT,),
        prompt_price=price, completion_price=price,
        price_source_kind=kw.pop("price_source_kind", "api"), **kw,
    )


@pytest.fixture
async def core(tmp_path):
    db = Database(str(tmp_path / "p.db"))
    await db.init()
    await db.upsert_service_provider("alpha", "Alpha", "alpha", None, enabled=True)
    await db.upsert_service_provider("beta", "Beta", "beta", None, enabled=False)
    for model in (
        _model("alpha", "alpha/cheap", 1e-6),
        _model("alpha", "alpha/mid", 3e-6),
        _model("alpha", "alpha/dear", 9e-6),
        _model("alpha", "alpha/priceless", 0.0, price_source_kind="unknown"),
        _model("beta", "beta/any", 2e-6),
    ):
        await db.upsert_registry_model(model)
    adapters = {"alpha": CountingAdapter("alpha"), "beta": CountingAdapter("beta")}
    registry = RegistryService(db, adapters=adapters)
    router = ModelRouter(db, registry, adapters, BlobStore(str(tmp_path / "b")))
    yield router, adapters, db
    await db.close()


@pytest.mark.asyncio
async def test_plan_never_leaves_the_host(core):
    router, adapters, _db = core
    await router.explain(RouterRequest(Task.TEXT_TO_TEXT, "u", text="hi"))
    assert all(a.catalog_calls == 0 for a in adapters.values())
    assert all(a.generate_calls == 0 for a in adapters.values())


@pytest.mark.asyncio
async def test_plan_answers_when_nothing_can_run(core):
    router, _adapters, db = core
    await db.set_provider_enabled("alpha", False)
    plan = await router.explain(RouterRequest(Task.TEXT_TO_TEXT, "u", text="hi"))
    # No exception, no empty screen: the reason is in the answer.
    assert plan["will_call"] is None
    assert plan["blocked"]
    assert all(c["verdict"] == "filtered" for c in plan["candidates"])
    assert any("выключен" in c["why"] for c in plan["candidates"])


@pytest.mark.asyncio
async def test_plan_names_the_problem_with_the_requested_model(core):
    router, _adapters, db = core
    await db.deprecate_registry_model("alpha", "alpha/mid")
    plan = await router.explain(
        RouterRequest(
            Task.TEXT_TO_TEXT, "u", text="hi", model_id="alpha/mid",
            fallback=FallbackPolicy(policy="cheaper"),
        )
    )
    assert "retired" in plan["problem"] or "снята" in plan["problem"]
    assert plan["will_call"]["model_id"] == "alpha/cheap"


@pytest.mark.asyncio
async def test_ladder_explains_every_rejection(core):
    router, _adapters, _db = core
    plan = await router.explain(
        RouterRequest(
            Task.TEXT_TO_TEXT, "u", text="hi", model_id="alpha/mid",
            fallback=FallbackPolicy(policy="cheaper"),
        )
    )
    by_id = {c["model_id"]: c for c in plan["candidates"]}
    # Kept: cheaper, same provider, priced.
    assert by_id["alpha/cheap"]["verdict"] == "eligible"
    # Dropped, each for a reason the operator can read.
    assert by_id["alpha/dear"]["why"] == "не дешевле опорной"
    assert by_id["alpha/priceless"]["why"] == "цены нет, на лестницу не ставится"
    assert by_id["beta/any"]["why"] in ("вне области подстановки", "поставщик выключен")


@pytest.mark.asyncio
async def test_estimate_states_its_assumption(core):
    router, _adapters, _db = core
    plan = await router.explain(
        RouterRequest(Task.TEXT_TO_TEXT, "u", text="x" * 400, model_id="alpha/cheap")
    )
    estimate = plan["estimate"]
    assert estimate["value"] > 0
    assert estimate["source"] == "computed"
    # The answer's length cannot be known in advance, so the number says what it
    # assumed rather than implying precision.
    assert "ответ" in estimate["assumption"]


@pytest.mark.asyncio
async def test_estimate_refuses_to_invent_a_zero(core):
    """For a model with no price the honest answer is "неизвестна" — a confident
    zero is how a screen lies (§7.5)."""
    router, _adapters, _db = core
    plan = await router.explain(
        RouterRequest(Task.TEXT_TO_TEXT, "u", text="hi", model_id="alpha/priceless")
    )
    assert plan["estimate"]["value"] is None
    assert plan["estimate"]["source"] == "unpriced"
