"""The Run envelope must look the same whichever way it arrives.

The console inspects a run right after sending it, when opening a row in the log,
and inside the multi-model table — all three with one component. That only holds
while `run_from_dispatch` and `run_from_log` produce the same shape, and the two
are written far apart. This test is the contract between them.
"""

from __future__ import annotations

import pytest

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.errors import NotConfigured, ProviderUnavailable
from router_mcp.domain.messages import (
    FallbackPolicy,
    RouterRequest,
    RouterResponse,
    Usage,
)
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database
from router_mcp.web.views import error_envelope, run_from_dispatch, run_from_log


class StubAdapter(ProviderAdapter):
    name = "stub"
    supported_tasks = {Task.TEXT_TO_TEXT}
    SUPPORTED_PARAMS = {Task.TEXT_TO_TEXT: ("temperature",)}

    def is_configured(self) -> bool:
        return True

    async def generate(self, request, model):
        return RouterResponse(
            Task.TEXT_TO_TEXT, "stub", model.model_id,
            Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cache_read_tokens=4),
            text="hi",
            raw_meta={"finish_reason": "stop", "request_id": "req-42"},
        )


async def _dispatch(tmp_path, *, text="ping", **request_kwargs):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.upsert_service_provider("stub", "Stub", "stub", None, enabled=True)
    model = RegistryModel(
        "stub", "stub", "stub/model", "Stub", (Modality.CHAT,),
        prompt_price=1e-6, completion_price=2e-6,
    )
    await db.upsert_registry_model(model)
    registry = RegistryService(db, adapters={})
    router = ModelRouter(db, registry, {"stub": StubAdapter()}, BlobStore(str(tmp_path / "b")))
    request = RouterRequest(
        Task.TEXT_TO_TEXT, "u", model_id="stub/model", text=text, **request_kwargs
    )
    result = await router.dispatch(request)
    return db, request, result


def _shape(value):
    """Key structure only — values differ between a live run and a stored one."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    return None


@pytest.mark.asyncio
async def test_both_constructors_agree_on_the_envelope(tmp_path):
    db, request, result = await _dispatch(tmp_path)
    try:
        from_run = run_from_dispatch(result, request)
        row = await db.get_request_log(result.log_id)
        from_log = run_from_log(row)
        assert _shape(from_run) == _shape(from_log)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_envelope_carries_what_the_screen_shows(tmp_path):
    db, request, result = await _dispatch(tmp_path, channel="ui", tool=None, params={"seed": 7})
    try:
        run = run_from_dispatch(result, request)
        assert run["model"] == {
            "provider": "stub", "model_id": "stub/model", "display_name": "stub/model",
        }
        assert run["usage"]["cache_read"] == 4
        assert run["cost"]["source"]  # money never travels without provenance
        assert run["latency_ms"] is not None
        assert run["finish_reason"] == "stop"
        assert run["provider_request_id"] == "req-42"
        assert run["channel"] == "ui"
        # The adapter reads `temperature`, not `seed` — say so instead of
        # dropping it silently.
        assert run["ignored_params"] == ["seed"]
        # The timeline must at least name the plan, the attempt and the price.
        assert {s["kind"] for s in run["steps"]} >= {"plan", "attempt", "cost", "log"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_request_side_survives_the_round_trip(tmp_path):
    """What was asked has to be readable from the entry, not only what came back."""
    db, request, result = await _dispatch(
        tmp_path,
        params={"temperature": 0.2},
        messages=[{"role": "user", "content": "ping"}],
        client_request_id="key-1",
        fallback=FallbackPolicy(policy="cheaper", scope="vendor", max_steps=2),
    )
    try:
        for run in (
            run_from_dispatch(result, request),
            run_from_log(await db.get_request_log(result.log_id)),
        ):
            asked = run["request"]
            assert asked["text"] == "ping"
            assert asked["params"] == {"temperature": 0.2}
            assert asked["messages"] == [{"role": "user", "content": "ping"}]
            # The caller's model, not the one that ran: the pair is on `model`.
            assert asked["model_id"] == "stub/model"
            assert asked["fallback"] == {
                "policy": "cheaper", "scope": "vendor", "max_steps": 2,
            }
            assert asked["client_request_id"] == "key-1"
    finally:
        await db.close()


def test_old_row_shows_the_text_it_has():
    """Entries written before the snapshot column keep the same key set."""
    row = {"id": 7, "status": "ok", "request_text": "старый запрос"}
    asked = run_from_log(row)["request"]
    assert asked["text"] == "старый запрос"
    assert asked["params"] == {} and asked["messages"] is None
    assert set(asked) == set(run_from_log({"id": 8})["request"])


@pytest.mark.asyncio
async def test_long_prompt_is_cut_and_says_so(tmp_path):
    db, request, result = await _dispatch(tmp_path, text="x" * 5000)
    try:
        asked = run_from_log(await db.get_request_log(result.log_id))["request"]
        assert len(asked["text"]) == 4000
        assert asked["text_truncated"] is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_log_row_round_trips(tmp_path):
    db, _request, result = await _dispatch(tmp_path)
    try:
        run = run_from_log(await db.get_request_log(result.log_id))
        assert run["log_id"] == result.log_id
        assert run["status"] == "ok"
        assert run["text"] == "hi"
        assert run["usage"]["prompt"] == 10
        assert run["created_at"]
    finally:
        await db.close()


def test_error_envelope_names_the_next_action():
    exc = NotConfigured(
        "Provider 'elevenlabs' is not configured",
        provider="elevenlabs",
        hint={"kind": "env_var", "env_var": "ELEVENLABS_API_KEY", "target": "elevenlabs"},
    )
    exc.log_id = 4819
    body = error_envelope(exc)["error"]
    assert body["code"] == "not_configured"
    assert exc.http_status == 503
    # Without log_id the "open this error in the log" link has nowhere to point.
    assert body["log_id"] == 4819
    assert body["hint"]["env_var"] == "ELEVENLABS_API_KEY"
    assert body["retriable"] is False
    assert ProviderUnavailable("x").retriable is True


def test_model_row_keys_are_a_client_contract():
    """`GET /registry/models` is read by external clients (agentish) to offer
    per-task model choice: `input_map` keys tell it which attachments to
    collect, `extra_params` which knobs to show. Removing or renaming a key
    breaks a caller this repo cannot see, so the set is pinned."""
    from router_mcp.mcp_server.serialize import model_to_dict

    row = model_to_dict(RegistryModel(
        "stub", "stub", "stub/model", "Stub", (Modality.CHAT,),
        input_map={"image": "input_image", "mask": "mask"},
        extra_params=("scale",),
    ))
    assert {
        "service_provider", "model_provider", "model_id", "display_name",
        "tasks", "tier", "prompt_price", "completion_price", "image_price",
        "audio_price_per_second", "character_price", "compute_price_per_second",
        "price_source_kind", "price_is_unknown", "is_active",
        "input_map", "extra_params",
    } <= set(row)
    assert row["input_map"] == {"image": "input_image", "mask": "mask"}
    assert row["extra_params"] == ["scale"]
