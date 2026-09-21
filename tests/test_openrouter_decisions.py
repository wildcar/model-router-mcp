"""OpenRouter's decisions endpoint (TypeSafe Jev). No network.

`typesafe/jev-1.13` refuses `/chat/completions` («is a decisions model … Use
the /api/alpha/decisions endpoint instead», seen live 2026-09-21) and answers
on `POST /systemone` with probabilities, not prose. The contract under test:
the `text-to-decisions` task goes straight there, a string state is the
prompt and a structured one rides in `params["state"]`, `questions` is
checked for shape before any money moves, the answers come back both as JSON
text and in their own shape, and the cost is the registry arithmetic (input
tokens only — the endpoint gives the output away), which the provider's own
sum confirms to the cent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from router_mcp.adapters.openrouter import OpenRouterAdapter
from router_mcp.domain.errors import BadRequest, ModelUnavailable, ProviderError
from router_mcp.domain.messages import RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

BASE = "https://openrouter.test/api/v1"

QUESTIONS: dict[str, Any] = {
    "is_bug": {
        "type": "noul",
        "instructions": "Is the customer reporting a software defect?",
        "criteria": {"true": "Broken behaviour described", "false": "A question or a wish"},
    },
    "team": {
        "type": "choice",
        "instructions": "Which team should own this ticket?",
        "criteria": {"account": "Login", "frontend": "Rendering", "payments": "Checkout"},
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is this ticket?",
        "criteria": ["Can wait", "This week", "Blocking revenue right now"],
    },
}

# The live answer of 2026-09-21 for the example request, verbatim.
ANSWERS: dict[str, Any] = {
    "is_bug": {"type": "noul", "noul": 0.96},
    "team": {
        "type": "choice", "choice": "payments", "confidence": 0.65,
        "probabilities": {"account": 0, "payments": 0.77, "frontend": 0.23},
    },
    "urgency": {
        "type": "score", "score": 1.99, "confidence": 0.99,
        "legend": {"0": "Can wait", "1": "This week", "2": "Blocking revenue right now"},
        "probabilities": {"0": 0, "1": 0.01, "2": 0.99},
    },
}


def _body() -> dict[str, Any]:
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": ANSWERS,
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
        "id": "gen-dec-1789980210-csIVgDRGbLxnAxn1VWZo",
        "provider": "TypeSafe",
    }


def _adapter(responses: dict[str, httpx.Response]):
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[request.url.path.rsplit("/", 1)[-1]]

    adapter = OpenRouterAdapter(api_key="sk-test", base_url=BASE, retries=0)
    adapter._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(handle)
    )
    return adapter, calls


def _jev(model_id: str = "typesafe/jev-1.13") -> RegistryModel:
    return RegistryModel(
        service_provider_id="openrouter",
        model_provider="typesafe",
        model_id=model_id,
        display_name="TypeSafe: Jev 1.13",
        tasks=(Task.TEXT_TO_DECISIONS,),
        prompt_price=0.000000042,
        completion_price=0.0,
        context_length=32000,
        source="manual",
        price_source_kind="manual",
    )


def test_the_task_is_text_in_text_out_billed_as_chat():
    sig = Task.TEXT_TO_DECISIONS.signature
    assert sig.billing is Modality.CHAT
    assert _jev().modalities == (Modality.CHAT,)
    assert Task.from_str("text_to_decisions") is Task.TEXT_TO_DECISIONS


@pytest.mark.asyncio
async def test_a_string_state_goes_to_systemone_as_the_prompt():
    adapter, calls = _adapter({"systemone": httpx.Response(200, json=_body())})
    request = RouterRequest(
        Task.TEXT_TO_DECISIONS, "u",
        text="My checkout page shows a blank screen after I click Pay.",
        params={"questions": QUESTIONS, "temperature": 0.7},
        app_url="https://agentish.example",
    )
    response = await adapter.generate(request, _jev())

    assert [c.url.path for c in calls] == ["/api/v1/systemone"]
    payload = json.loads(calls[0].content.decode())
    assert payload == {
        "model": "typesafe/jev-1.13",
        "state": "My checkout page shows a blank screen after I click Pay.",
        "questions": QUESTIONS,
    }
    assert calls[0].headers["HTTP-Referer"] == "https://agentish.example"

    assert response.task is Task.TEXT_TO_DECISIONS
    assert json.loads(response.text) == ANSWERS
    assert response.raw_meta["answers"] == ANSWERS
    assert response.raw_meta["served_model_id"] == "typesafe/jev-1.13-20260917"
    assert response.raw_meta["provider_request_id"].startswith("gen-dec-")
    assert response.raw_meta["endpoint"] == "/systemone"
    assert response.usage.prompt_tokens == 476
    assert response.usage.completion_tokens == 70
    assert response.usage.total_tokens == 546
    # A text task: the registry rate is exact, so the provider's sum is kept as
    # the check and not booked in its place.
    assert response.cost_override_usd is None
    assert response.raw_meta["provider_cost_usd"] == pytest.approx(0.000019992)


@pytest.mark.asyncio
async def test_a_structured_state_travels_as_is():
    adapter, calls = _adapter({"systemone": httpx.Response(200, json=_body())})
    state = {"customer_tier": "enterprise", "ticket": "Blank screen after Pay."}
    request = RouterRequest(
        Task.TEXT_TO_DECISIONS, "u",
        text=json.dumps(state),  # what the log shows
        params={"questions": json.dumps(QUESTIONS), "state": state},
    )
    await adapter.generate(request, _jev("~typesafe/jev-latest"))
    payload = json.loads(calls[0].content.decode())
    assert payload["model"] == "~typesafe/jev-latest"
    assert payload["state"] == state
    # Questions handed over as their JSON text are parsed, not forwarded quoted.
    assert payload["questions"] == QUESTIONS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("questions", "fragment"),
    [
        (None, "non-empty JSON object"),
        ({}, "non-empty JSON object"),
        ("{not json", "not valid JSON"),
        ({"q": "yes?"}, "must be an object"),
        ({"q": {"type": "boolean", "instructions": "x"}}, "expected one of noul, choice, score"),
        ({"q": {"type": "noul"}}, "no `instructions`"),
        ({"q": {"type": "choice", "instructions": "x"}}, "needs `criteria`"),
        ({"q": {"type": "score", "instructions": "x", "criteria": []}}, "needs `criteria`"),
    ],
)
async def test_malformed_questions_are_refused_before_the_call(questions, fragment):
    adapter, calls = _adapter({})
    request = RouterRequest(
        Task.TEXT_TO_DECISIONS, "u", text="ticket", params={"questions": questions}
    )
    with pytest.raises(BadRequest) as info:
        await adapter.generate(request, _jev())
    assert fragment in str(info.value)
    assert calls == []


@pytest.mark.asyncio
async def test_a_noul_question_needs_no_criteria():
    adapter, calls = _adapter({"systemone": httpx.Response(200, json=_body())})
    request = RouterRequest(
        Task.TEXT_TO_DECISIONS, "u", text="I was charged twice.",
        params={"questions": {"refund": {"type": "noul", "instructions": "Refund asked?"}}},
    )
    await adapter.generate(request, _jev())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_state_at_all_is_a_caller_error():
    adapter, calls = _adapter({})
    with pytest.raises(BadRequest):
        await adapter.generate(
            RouterRequest(Task.TEXT_TO_DECISIONS, "u", params={"questions": QUESTIONS}),
            _jev(),
        )
    assert calls == []


@pytest.mark.asyncio
async def test_an_unknown_model_is_retired_not_a_generic_failure():
    """`Model typesafe/jev-9 does not exist` (HTTP 400, seen live) is the one
    4xx a fallback policy may answer by choosing another model."""
    adapter, _calls = _adapter({
        "systemone": httpx.Response(
            400, json={"error": {"message": "Model typesafe/jev-9 does not exist", "code": 400}}
        )
    })
    with pytest.raises(ModelUnavailable):
        await adapter.generate(
            RouterRequest(Task.TEXT_TO_DECISIONS, "u", text="t", params={"questions": QUESTIONS}),
            _jev("typesafe/jev-9"),
        )


@pytest.mark.asyncio
async def test_a_body_without_answers_is_an_empty_response():
    adapter, _calls = _adapter({
        "systemone": httpx.Response(200, json={"model": "x", "answers": {}, "usage": {}})
    })
    with pytest.raises(ProviderError, match="no answers"):
        await adapter.generate(
            RouterRequest(Task.TEXT_TO_DECISIONS, "u", text="t", params={"questions": QUESTIONS}),
            _jev(),
        )


def test_the_catalog_reads_decisions_as_its_own_task():
    """`/models/{id}/endpoints` describes Jev as `text->decisions`; a browsed
    row built from it must not pass for a chat model."""
    raw = {
        "id": "typesafe/jev-1.13",
        "name": "TypeSafe: Jev 1.13",
        "architecture": {
            "modality": "text->decisions",
            "input_modalities": ["text"],
            "output_modalities": ["decisions"],
        },
        "pricing": {"prompt": "0.000000042", "completion": "0"},
        "context_length": 32000,
    }
    model = OpenRouterAdapter._parse_catalog_model(raw)
    assert model is not None
    assert model.tasks == (Task.TEXT_TO_DECISIONS,)
    assert model.modalities == (Modality.CHAT,)
    assert model.prompt_price == pytest.approx(4.2e-8)
    assert model.tier == "cheap"
    # The legacy string alone is enough.
    legacy_only = {"id": "typesafe/jev-1.13", "architecture": {"modality": "text->decisions"}}
    assert OpenRouterAdapter._extract_tasks(legacy_only) == (Task.TEXT_TO_DECISIONS,)
    # A chat model with an empty architecture block is still a chat model.
    assert OpenRouterAdapter._extract_tasks({"id": "x/y"}) == (Task.TEXT_TO_TEXT,)


def test_a_tilde_alias_belongs_to_its_vendor():
    assert OpenRouterAdapter._extract_provider("~typesafe/jev-latest") == "typesafe"
    assert OpenRouterAdapter._extract_provider("typesafe/jev-1.13") == "typesafe"
    assert OpenRouterAdapter._extract_provider("bare-id") == "other"


@pytest.mark.asyncio
async def test_the_journal_prices_input_tokens_only_and_matches_the_provider(tmp_path):
    """End to end through the router: 476 input tokens × $0.042/M = the
    provider's own $0.000019992, `cost_source` is `computed` (a manual price),
    the reconciliation filter stays quiet, and the row confirms the model id."""
    from router_mcp.blobs.store import BlobStore
    from router_mcp.registry.service import RegistryService
    from router_mcp.routing.router import ModelRouter
    from router_mcp.storage.database import Database

    adapter, _calls = _adapter({"systemone": httpx.Response(200, json=_body())})
    db = Database(str(tmp_path / "dec.db"))
    await db.init()
    await db.upsert_service_provider("openrouter", "OpenRouter", "openrouter", None, enabled=True)
    await db.upsert_registry_model(_jev())
    router = ModelRouter(
        db, RegistryService(db, adapters={}), {"openrouter": adapter},
        BlobStore(str(tmp_path / "blobs")),
    )
    try:
        result = await router.dispatch(
            RouterRequest(
                Task.TEXT_TO_DECISIONS, "agentish-7",
                model_id="typesafe/jev-1.13",
                text="Blank screen after Pay.",
                params={"questions": QUESTIONS},
                channel="mcp", tool="decide",
            )
        )
        assert result.cost.cost_source == "computed"
        assert result.cost.cost_usd == pytest.approx(0.000019992)
        assert result.ignored_params == []

        row = await db.get_request_log(result.log_id)
        assert row is not None
        assert row["task"] == "text-to-decisions"
        assert row["modality"] == "chat"
        assert row["prompt_tokens"] == 476 and row["completion_tokens"] == 70
        assert row["provider_cost_usd"] == pytest.approx(0.000019992)
        assert json.loads(row["response_text"]) == ANSWERS
        assert [r["id"] for r in await db.list_request_logs(cost_mismatch=True)] == []

        stored = await db.get_registry_model_by_id("typesafe/jev-1.13", active_only=False)
        assert stored is not None and stored.confirmed_at is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_chat_model_cannot_be_asked_to_decide(tmp_path):
    """The task is declared, never inferred: a chat row pinned for a decisions
    request is a caller mistake, refused before any call."""
    from router_mcp.blobs.store import BlobStore
    from router_mcp.domain.errors import RouterError
    from router_mcp.registry.service import RegistryService
    from router_mcp.routing.router import ModelRouter
    from router_mcp.storage.database import Database

    adapter, calls = _adapter({})
    db = Database(str(tmp_path / "dec2.db"))
    await db.init()
    await db.upsert_service_provider("openrouter", "OpenRouter", "openrouter", None, enabled=True)
    await db.upsert_registry_model(RegistryModel(
        service_provider_id="openrouter", model_provider="openai",
        model_id="openai/gpt-4o", display_name="gpt-4o",
        modalities=(Modality.CHAT,), prompt_price=1e-7, completion_price=1e-7,
        source="manual", price_source_kind="manual",
    ))
    router = ModelRouter(
        db, RegistryService(db, adapters={}), {"openrouter": adapter},
        BlobStore(str(tmp_path / "blobs")),
    )
    try:
        with pytest.raises(RouterError, match="does not support task text-to-decisions"):
            await router.dispatch(RouterRequest(
                Task.TEXT_TO_DECISIONS, "u", model_id="openai/gpt-4o",
                text="t", params={"questions": QUESTIONS},
            ))
        assert calls == []
    finally:
        await db.close()


# --- the HTTP surface ------------------------------------------------------

def test_the_console_learns_the_task_and_its_json_fields(client):
    caps = client.get("/api/v1/capabilities").json()
    row = caps["tasks"]["text-to-decisions"]
    assert row["available"] is True
    assert row["billing_group"] == "chat"
    assert row["label"] == "Текст в решение"
    params = {p["name"]: p for p in caps["params_by_task"]["text-to-decisions"]["openrouter"]["params"]}
    assert params["questions"]["type"] == "json"
    assert params["state"]["type"] == "json"


def test_the_dry_run_knows_the_task(client):
    plan = client.post(
        "/api/v1/plan",
        json={"task": "text-to-decisions", "text": "ticket", "params": {"questions": QUESTIONS}},
    ).json()
    assert plan["task"] == "text-to-decisions"
    assert plan["modality"] == "chat"


def test_decide_without_a_state_is_the_callers_mistake(client):
    r = client.post("/api/v1/decide", json={"questions": QUESTIONS})
    assert r.status_code == 400
    assert "state" in r.json()["error"]["message"]


def test_decide_with_no_provider_configured_fails_in_the_router_not_in_the_adapter(client):
    """The plumbing: `/decide` builds a `text-to-decisions` request and the
    router answers for it — no key in tests, so the ladder is empty."""
    r = client.post(
        "/api/v1/decide",
        json={"state": {"ticket": "Blank screen after Pay."}, "questions": QUESTIONS},
    )
    assert r.status_code >= 400
    assert "text-to-decisions" in r.json()["error"]["message"]


def test_runs_accepts_the_decisions_shorthand(client):
    """`questions` and `state` at the top level, as on `/decide`; the console
    sends the same two inside `params`. Either way the run is accepted."""
    r = client.post(
        "/api/v1/runs",
        json={
            "task": "text-to-decisions",
            "external_user_id": "dec-shorthand",
            "state": "I was charged twice.",
            "questions": QUESTIONS,
        },
    )
    assert r.status_code == 202, r.text
    r = client.post(
        "/api/v1/runs",
        json={
            "task": "text-to-decisions",
            "external_user_id": "dec-params",
            "text": "I was charged twice.",
            "params": {"questions": QUESTIONS, "state": {"ticket": "I was charged twice."}},
        },
    )
    assert r.status_code == 202, r.text
    # A decisions run with nothing to judge is refused by the task's signature.
    r = client.post(
        "/api/v1/runs",
        json={"task": "text-to-decisions", "params": {"questions": QUESTIONS}},
    )
    assert r.status_code == 400
    assert "text" in r.json()["error"]["message"]
