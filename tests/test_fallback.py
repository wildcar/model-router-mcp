"""Fallback policy: when a pinned model is dead, which one runs instead.

The rule these tests defend: a substitution is allowed, a silent substitution is
not. Every path that swaps a model must report `requested_model_id` and
`fallback_reason` in the result and in the log.
"""

from __future__ import annotations

import pytest

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.errors import ModelUnavailable, ProviderError, RouterError
from router_mcp.domain.messages import (
    FallbackPolicy,
    RouterRequest,
    RouterResponse,
    Usage,
)
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database


class ScriptedAdapter(ProviderAdapter):
    """Answers normally unless a model id is listed in `broken`."""

    name = "vendor"
    supported_modalities = {Modality.CHAT}

    def __init__(self, broken: dict[str, Exception] | None = None) -> None:
        self.broken = broken or {}
        self.calls: list[str] = []

    def is_configured(self) -> bool:
        return True

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        self.calls.append(model.model_id)
        if model.model_id in self.broken:
            raise self.broken[model.model_id]
        return RouterResponse(
            modality=Modality.CHAT,
            provider=self.name,
            model_id=model.model_id,
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            text=f"answered by {model.model_id}",
        )


def _model(model_id: str, price: float, *, active: bool = True) -> RegistryModel:
    return RegistryModel(
        service_provider_id="vendor",
        model_provider="vendor",
        model_id=model_id,
        display_name=model_id,
        modalities=(Modality.CHAT,),
        tier="cheap",
        prompt_price=price,
        completion_price=price,
        source="manual",
        price_source_kind="manual",
        is_active=active,
    )


async def _core(tmp_path, adapter: ScriptedAdapter, models: list[RegistryModel]):
    db = Database(str(tmp_path / "fb.db"))
    await db.init()
    await db.upsert_service_provider("vendor", "Vendor", "vendor", None, enabled=True)
    for m in models:
        await db.upsert_registry_model(m)
    registry = RegistryService(db, adapters={})
    router = ModelRouter(db, registry, {"vendor": adapter}, BlobStore(str(tmp_path / "b")))
    return db, router


# The ladder used by most tests: 1x, 2x, 4x, 8x.
def _ladder() -> list[RegistryModel]:
    return [
        _model("flash", 1e-7),
        _model("mid", 2e-7),
        _model("pro", 4e-7),
        _model("max", 8e-7),
    ]


@pytest.mark.asyncio
async def test_retired_model_without_policy_still_fails(tmp_path):
    """Default behaviour is unchanged: no policy, no substitution."""
    db, router = await _core(tmp_path, ScriptedAdapter(), _ladder() + [_model("old", 2e-7, active=False)])
    try:
        req = RouterRequest(
            modality=Modality.CHAT, external_user_id="u", model_id="old", text="hi"
        )
        with pytest.raises(RouterError, match="retired"):
            await router.dispatch(req)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_policy_picks_the_nearest_price(tmp_path):
    adapter = ScriptedAdapter()
    db, router = await _core(tmp_path, adapter, _ladder() + [_model("old", 3.5e-7, active=False)])
    try:
        req = RouterRequest(
            modality=Modality.CHAT,
            external_user_id="u",
            model_id="old",
            text="hi",
            fallback=FallbackPolicy(policy="same"),
        )
        result = await router.dispatch(req)

        assert result.response.model_id == "pro"  # 4e-7 is nearest to 3.5e-7
        assert result.requested_model_id == "old"
        assert "retired" in result.fallback_reason
        logs = await db.list_request_logs(external_user_id="u")
        assert logs[0]["requested_model_id"] == "old"
        assert logs[0]["model_id"] == "pro"
        assert logs[0]["fallback_reason"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cheaper_and_pricier_step_one_rung(tmp_path):
    for policy, expected in (("cheaper", "mid"), ("pricier", "max")):
        adapter = ScriptedAdapter()
        db, router = await _core(
            tmp_path / policy, adapter, _ladder() + [_model("old", 4e-7, active=False)]
        )
        try:
            req = RouterRequest(
                modality=Modality.CHAT,
                external_user_id="u",
                model_id="old",
                text="hi",
                fallback=FallbackPolicy(policy=policy),
            )
            result = await router.dispatch(req)
            # "pro" also sits at 4e-7, but a step must move: strict comparison.
            assert result.response.model_id == expected
        finally:
            await db.close()


@pytest.mark.asyncio
async def test_runtime_model_not_found_falls_back(tmp_path):
    """The provider, not the registry, is the one that knows the id is dead."""
    adapter = ScriptedAdapter({"pro": ModelUnavailable("HTTP 400: model not found")})
    db, router = await _core(tmp_path, adapter, _ladder())
    try:
        req = RouterRequest(
            modality=Modality.CHAT,
            external_user_id="u",
            model_id="pro",
            text="hi",
            fallback=FallbackPolicy(policy="cheaper"),
        )
        result = await router.dispatch(req)

        assert adapter.calls == ["pro", "mid"]
        assert result.response.model_id == "mid"
        assert result.requested_model_id == "pro"
        assert "model not found" in result.fallback_reason
        # Both the failed attempt and the successful one are on record. The
        # run's own row is opened when the run starts, so it sorts *before* the
        # attempt failure it later recovered from: the log is ordered by when a
        # request began, not by when it finished.
        logs = await db.list_request_logs(external_user_id="u")
        assert [(row["model_id"], row["status"]) for row in logs] == [
            ("pro", "error"),
            ("mid", "ok"),
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prompt_level_errors_are_not_retried_elsewhere(tmp_path):
    """A refusal or a bad prompt fails the same way on every model; retrying
    would just spend money twice."""
    adapter = ScriptedAdapter({"pro": ProviderError("invalid request: bad role")})
    db, router = await _core(tmp_path, adapter, _ladder())
    try:
        req = RouterRequest(
            modality=Modality.CHAT,
            external_user_id="u",
            model_id="pro",
            text="hi",
            fallback=FallbackPolicy(policy="same", max_steps=3),
        )
        with pytest.raises(ProviderError):
            await router.dispatch(req)
        assert adapter.calls == ["pro"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_max_steps_bounds_the_walk(tmp_path):
    adapter = ScriptedAdapter(
        {m: ModelUnavailable("gone") for m in ("pro", "mid", "flash", "max")}
    )
    db, router = await _core(tmp_path, adapter, _ladder())
    try:
        req = RouterRequest(
            modality=Modality.CHAT,
            external_user_id="u",
            model_id="pro",
            text="hi",
            fallback=FallbackPolicy(policy="same", max_steps=2),
        )
        with pytest.raises(ModelUnavailable):
            await router.dispatch(req)
        assert len(adapter.calls) == 3  # the original plus two substitutes
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unknown_model_id_falls_back_to_cheapest(tmp_path):
    adapter = ScriptedAdapter()
    db, router = await _core(tmp_path, adapter, _ladder())
    try:
        req = RouterRequest(
            modality=Modality.CHAT,
            external_user_id="u",
            model_id="never-existed",
            text="hi",
            fallback=FallbackPolicy(policy="same"),
        )
        result = await router.dispatch(req)
        assert result.response.model_id == "flash"
        assert "not in the registry" in result.fallback_reason
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unpriced_models_are_not_treated_as_cheapest(tmp_path):
    """A catalog that lists ids without prices leaves zeros in the row. Those
    zeros must not win the automatic pick or land on the price ladder."""
    unpriced = RegistryModel(
        service_provider_id="vendor",
        model_provider="vendor",
        model_id="unpriced",
        display_name="unpriced",
        modalities=(Modality.CHAT,),
        tier="cheap",
        source="catalog",
        price_source_kind="unknown",
    )
    adapter = ScriptedAdapter()
    db, router = await _core(tmp_path, adapter, _ladder() + [unpriced])
    try:
        # Automatic selection: the cheapest *known* price wins.
        picked = await router.dispatch(
            RouterRequest(modality=Modality.CHAT, external_user_id="u", text="hi")
        )
        assert picked.response.model_id == "flash"

        # Fallback ladder: never steps onto the unpriced row.
        result = await router.dispatch(
            RouterRequest(
                modality=Modality.CHAT,
                external_user_id="u",
                model_id="flash",
                text="hi",
                fallback=FallbackPolicy(policy="cheaper", max_steps=3),
            )
        )
        assert result.response.model_id == "flash"  # nothing cheaper is known
    finally:
        await db.close()


def test_policy_parsing_rejects_nonsense():
    assert FallbackPolicy.parse(None) is None
    assert FallbackPolicy.parse("cheaper").policy == "cheaper"
    assert FallbackPolicy.parse({"policy": "same", "max_steps": 99}).max_steps == 5
    with pytest.raises(ValueError):
        FallbackPolicy.parse({"policy": "cheapest"})
    with pytest.raises(ValueError):
        FallbackPolicy.parse({"policy": "same", "scope": "galaxy"})
