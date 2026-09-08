"""codex-oauth: the text model that leads an image request comes from the
registry, and a backend refusal retires the row it names (ADR-0010).

The scenario is prod log 21752..21754 (2026-09-08): `CODEX_IMAGE_MAIN_MODEL`
pointed at gpt-5.5, `GET /models` still listed it, `/responses` answered
HTTP 404 model_not_found, and three image requests died before any drawing
started. The backend here is a fake `/responses` that refuses one slug and
draws on another.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from router_mcp.adapters.codex_oauth import CodexOAuthAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.errors import ModelUnavailable
from router_mcp.domain.messages import RouterRequest
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database

PROVIDER = "codex-oauth"
PNG = b"\x89PNG\r\n\x1a\n fake picture bytes"

NOT_FOUND = {
    "error": {
        "code": "model_not_found",
        "message": "The model gpt-5.5 does not exist or you do not have access to it",
        "type": "invalid_request_error",
    }
}
NOT_SUPPORTED = {
    "detail": "The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account."
}


def _row(model_id: str, task: Task, **kw) -> RegistryModel:
    base = dict(
        service_provider_id=PROVIDER,
        model_provider="openai",
        model_id=model_id,
        display_name=model_id,
        tasks=(task,),
        tier="expensive",
        source="catalog",
        price_source_kind="unknown",
    )
    base.update(kw)
    return RegistryModel(**base)


class FakeResponses:
    """`/responses` that refuses the slugs in `dead` and draws on the rest."""

    def __init__(self, dead: dict[str, tuple[int, dict]]) -> None:
        self.dead = dead
        self.models: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        body = json.loads(request.content)
        model = body["model"]
        self.models.append(model)
        if model in self.dead:
            status, payload = self.dead[model]
            return httpx.Response(status, json=payload)
        if body.get("tools"):
            event = {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": base64.b64encode(PNG).decode(),
                        }
                    ],
                    "usage": {},
                },
            }
        else:
            event = {
                "type": "response.completed",
                "response": {
                    "id": "resp_2",
                    "status": "completed",
                    "output": [{"content": [{"text": f"hello from {model}"}]}],
                    "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                },
            }
        events = [event]
        if not body.get("tools"):
            # Text arrives as deltas; the adapter joins them.
            events.insert(0, {"type": "response.output_text.delta", "delta": f"hello from {model}"})
        sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=sse.encode(), headers={"content-type": "text/event-stream"}
        )


async def _setup(tmp_path, backend: FakeResponses, rows: list[RegistryModel], *, main="gpt-5.5"):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok", "refresh_token": "ref"}}))
    db = Database(str(tmp_path / "codex.db"))
    await db.init()
    await db.upsert_service_provider(PROVIDER, "Codex (OAuth)", "codex_oauth", None, enabled=True)
    for row in rows:
        await db.upsert_registry_model(row)
    adapter = CodexOAuthAdapter(
        auth_path=str(auth),
        base_url="https://codex.invalid",
        main_model=main,
        image_model="gpt-image-2",
        size="1024x1536",
        quality="medium",
        retries=0,
    )
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(backend))
    registry = RegistryService(db, adapters={PROVIDER: adapter})
    adapter.bind_registry(registry)
    return db, registry, adapter


def _image_request(**kw) -> RouterRequest:
    return RouterRequest(
        task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот в очках", **kw
    )


def _ladder_rows() -> list[RegistryModel]:
    return [
        _row("gpt-5.5", Task.TEXT_TO_TEXT),
        _row("gpt-5.6-sol", Task.TEXT_TO_TEXT),
        _row("gpt-5.6-luna", Task.TEXT_TO_TEXT),
        _row("gpt-image-2", Task.TEXT_TO_IMAGE),
    ]


@pytest.mark.asyncio
async def test_refused_leader_is_retired_and_the_picture_is_drawn_by_the_next(tmp_path):
    """The acceptance case: configured leader dead upstream, catalog still
    listing it, one retry with a live leader, the row retired and the
    substitution written down."""
    backend = FakeResponses({"gpt-5.5": (404, NOT_FOUND)})
    db, registry, adapter = await _setup(tmp_path, backend, _ladder_rows())
    try:
        # sol answered once before; luna never did. The confirmed one leads.
        await db.confirm_registry_model(PROVIDER, "gpt-5.6-sol")
        image_row = await db.get_registry_model(PROVIDER, "gpt-image-2")

        response = await adapter.generate(_image_request(), image_row)

        assert backend.models == ["gpt-5.5", "gpt-5.6-sol"]
        assert response.attachments[0].data == PNG
        assert response.usage.images == 1
        assert response.usage.raw["main_model"] == "gpt-5.6-sol"
        reason = response.raw_meta["fallback_reason"]
        assert "gpt-5.5 failed" in reason and "404" in reason and "gpt-5.6-sol" in reason
        assert response.raw_meta["requested_main_model"] == "gpt-5.5"

        dead = await db.get_registry_model(PROVIDER, "gpt-5.5")
        assert dead.is_active is False
        assert dead.retired_by == "backend"
        assert dead.replaced_by == "gpt-5.6-sol"
        assert dead.deprecated_at
        # The image row itself did answer, and stays live.
        assert (await db.get_registry_model(PROVIDER, "gpt-image-2")).is_active
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_leader_already_retired_in_the_registry_is_not_even_asked(tmp_path):
    """gpt-5.4 (2026-09-05): the catalog retired the row and the image path
    kept sending the slug anyway. Now the ladder starts at a live one."""
    backend = FakeResponses({"gpt-5.5": (404, NOT_FOUND)})
    rows = _ladder_rows()
    db, registry, adapter = await _setup(tmp_path, backend, rows)
    try:
        await db.deprecate_registry_model(PROVIDER, "gpt-5.5")
        image_row = await db.get_registry_model(PROVIDER, "gpt-image-2")

        response = await adapter.generate(_image_request(), image_row)

        # Nothing confirmed, so alphabetical: luna before sol.
        assert backend.models == ["gpt-5.6-luna"]
        assert "retired in the registry" in response.raw_meta["fallback_reason"]
        assert response.raw_meta["main_model"] == "gpt-5.6-luna"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_configured_leader_leads_while_its_row_is_active(tmp_path):
    backend = FakeResponses({})
    db, registry, adapter = await _setup(tmp_path, backend, _ladder_rows(), main="gpt-5.6-sol")
    try:
        image_row = await db.get_registry_model(PROVIDER, "gpt-image-2")
        response = await adapter.generate(_image_request(), image_row)
        assert backend.models == ["gpt-5.6-sol"]
        assert "fallback_reason" not in response.raw_meta
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_live_leader_left_fails_with_a_hint_that_names_the_variable(tmp_path):
    """Prod 21752 showed `hint: null` and a 404 about a model the caller never
    named. The error now says which slug died and what to set."""
    backend = FakeResponses({"gpt-5.5": (404, NOT_FOUND)})
    rows = [_row("gpt-5.5", Task.TEXT_TO_TEXT), _row("gpt-image-2", Task.TEXT_TO_IMAGE)]
    db, registry, adapter = await _setup(tmp_path, backend, rows)
    try:
        image_row = await db.get_registry_model(PROVIDER, "gpt-image-2")
        with pytest.raises(ModelUnavailable) as info:
            await adapter.generate(_image_request(), image_row)
        exc = info.value
        assert exc.model_id == "gpt-5.5"
        assert exc.provider_status == 404
        assert exc.hint["env_var"] == "CODEX_IMAGE_MAIN_MODEL"
        assert exc.hint["retired"] == ["gpt-5.5"]
        assert "no active text model" in exc.hint["action"]
        assert backend.models == ["gpt-5.5"]
        assert (await db.get_registry_model(PROVIDER, "gpt-5.5")).retired_by == "backend"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_two_refusals_in_a_row_stop_there(tmp_path):
    backend = FakeResponses({"gpt-5.5": (404, NOT_FOUND), "gpt-5.6-luna": (400, NOT_SUPPORTED)})
    db, registry, adapter = await _setup(tmp_path, backend, _ladder_rows())
    try:
        image_row = await db.get_registry_model(PROVIDER, "gpt-image-2")
        with pytest.raises(ModelUnavailable) as info:
            await adapter.generate(_image_request(), image_row)
        # Leader, one replacement, no third call even though sol is live.
        assert backend.models == ["gpt-5.5", "gpt-5.6-luna"]
        assert info.value.hint["retired"] == ["gpt-5.5", "gpt-5.6-luna"]
        retired = {
            m.model_id
            for m in await db.list_registry_models(service_provider_id=PROVIDER, active_only=False)
            if m.retired_by == "backend"
        }
        assert retired == {"gpt-5.5", "gpt-5.6-luna"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_text_request_on_a_dead_slug_retires_it_too(tmp_path):
    """Prod 21759: chat pinned to gpt-5.5 got the same 404. It is a
    `ModelUnavailable` now — the one 4xx a fallback policy may act on — and
    the row does not wait for a catalog that never stopped listing it."""
    backend = FakeResponses({"gpt-5.5": (404, NOT_FOUND)})
    db, registry, adapter = await _setup(tmp_path, backend, _ladder_rows())
    try:
        row = await db.get_registry_model(PROVIDER, "gpt-5.5")
        request = RouterRequest(task=Task.TEXT_TO_TEXT, external_user_id="u", text="hi")
        with pytest.raises(ModelUnavailable) as info:
            await adapter.generate(request, row)
        assert info.value.hint == {
            "kind": "pick_model",
            "retired": "gpt-5.5",
            "action": "choose an active model of codex-oauth",
        }
        assert (await db.get_registry_model(PROVIDER, "gpt-5.5")).retired_by == "backend"
        # A live slug still answers, and confirms itself.
        live = await db.get_registry_model(PROVIDER, "gpt-5.6-sol")
        response = await adapter.generate(request, live)
        assert response.text == "hello from gpt-5.6-sol"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_router_logs_the_substitution(tmp_path):
    """End to end: the log row says gpt-image-2 answered, and why not the way
    the configuration said."""
    backend = FakeResponses({"gpt-5.5": (404, NOT_FOUND)})
    db, registry, adapter = await _setup(tmp_path, backend, _ladder_rows())
    try:
        await db.confirm_registry_model(PROVIDER, "gpt-5.6-sol")
        router = ModelRouter(db, registry, {PROVIDER: adapter}, BlobStore(str(tmp_path / "blobs")))
        result = await router.dispatch(
            _image_request(model_id="gpt-image-2", provider_hint=PROVIDER)
        )
        assert result.response.model_id == "gpt-image-2"
        assert result.requested_model_id == "gpt-image-2"
        assert "gpt-5.5 failed" in result.fallback_reason
        assert any(s["kind"] == "fallback" for s in result.steps)

        logs = await db.list_request_logs(external_user_id="u")
        assert [(r["model_id"], r["status"]) for r in logs] == [("gpt-image-2", "ok")]
        assert logs[0]["images"] == 1
        assert "gpt-5.6-sol" in logs[0]["fallback_reason"]
    finally:
        await db.close()


@pytest.mark.parametrize(
    ("status", "body", "gone"),
    [
        (404, json.dumps(NOT_FOUND), True),
        (400, json.dumps(NOT_SUPPORTED), True),
        (400, '{"error": {"message": "The model `x` does not exist"}}', True),
        (404, "no such route", False),
        (400, '{"error": {"message": "System messages are not allowed"}}', False),
        (400, "Unsupported value: reasoning.effort 'ultra'", False),
        (422, "model not found", False),
    ],
)
def test_which_answers_mean_the_model_is_gone(status, body, gone):
    assert CodexOAuthAdapter._model_gone(status, body) is gone


def test_without_a_registry_the_configured_slug_is_the_whole_ladder():
    """Bare adapter, as in the parse tests: nothing to consult, nothing to retire."""
    adapter = CodexOAuthAdapter(
        auth_path=None, base_url="https://x.invalid", main_model="gpt-5.5",
        image_model="gpt-image-2", size="1024x1536", quality="medium",
    )
    import asyncio

    assert asyncio.run(adapter._main_model_ladder()) == (["gpt-5.5"], None)
    assert adapter._payload("кот", size="1024x1024", quality="high")["model"] == "gpt-5.5"
    assert adapter._payload("кот", size="1024x1024", quality="high", main_model="gpt-5.6-sol")["model"] == "gpt-5.6-sol"
