"""The parameter matrix must come from the adapters, not from a copy of them.

A form that lists fields the adapter ignores teaches the operator to distrust
the screen; a form that hides fields the adapter reads makes the API reachable
only by curl. Both happen the moment `web/` keeps its own table of parameters,
which is why the matrix is derived from `SUPPORTED_PARAMS` and this test walks
back from the answer to the code (§5 of docs/UI_CONCEPT_v2.md).
"""

from __future__ import annotations

from router_mcp.adapters.base import ProviderAdapter, ignored_params
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.runtime import get_core
from router_mcp.web import capabilities


def test_matrix_matches_the_adapters(client):
    caps = client.get("/api/v1/capabilities").json()
    adapters = get_core().adapters
    for task_value, per_provider in caps["params_by_task"].items():
        task = Task.from_str(task_value)
        for provider_id, spec in per_provider.items():
            declared = adapters[provider_id].SUPPORTED_PARAMS[task]
            assert [p["name"] for p in spec["params"]] == list(declared)


def test_every_adapter_declares_its_params(client):
    """A new adapter without a declaration would silently show no parameters."""
    missing = [
        f"{provider_id}:{task.value}"
        for provider_id, adapter in get_core().adapters.items()
        for task in adapter.supported_tasks
        if adapter.SUPPORTED_PARAMS is None
        or task not in adapter.SUPPORTED_PARAMS
    ]
    assert not missing, f"adapters with no SUPPORTED_PARAMS entry: {missing}"


def test_every_task_is_described(client):
    """The console labels and validates a task from this block, so a task with no
    entry would render as a bare identifier with no input fields."""
    tasks = client.get("/api/v1/capabilities").json()["tasks"]
    assert set(tasks) == {t.value for t in Task}
    for value, row in tasks.items():
        assert row["label"] and row["help"], value
        assert row["produces"], value
        assert row["billing_group"]


def test_a_task_nobody_provides_is_shown_as_unavailable(client):
    """Hiding it would leave the operator unable to tell "we cannot do this" from
    "nobody configured it"."""
    tasks = client.get("/api/v1/capabilities").json()["tasks"]
    # Nothing here edits an image yet — that is the point of the reported defect.
    assert tasks["image-to-image"]["available"] is False
    assert tasks["text-to-image"]["available"] is True


def test_codex_oauth_does_not_claim_it_can_edit_an_image(client):
    """The defect this taxonomy exists for: producing an image says nothing about
    being able to change one."""
    adapter = get_core().adapters["codex-oauth"]
    assert Task.TEXT_TO_IMAGE in adapter.supported_tasks
    assert Task.IMAGE_TO_IMAGE not in adapter.supported_tasks


def test_params_carry_a_visible_explanation(client):
    """§3: a term first appearing on screen gets text under the control, not a
    tooltip. Empty help would leave the operator guessing at `top_p`."""
    caps = client.get("/api/v1/capabilities").json()
    for per_provider in caps["params_by_task"].values():
        for spec in per_provider.values():
            for param in spec["params"]:
                assert param["label"], param["name"]
                assert param["help"], param["name"]


def test_cwd_is_a_closed_list(client):
    """Free-form cwd is arbitrary-directory execution behind one password."""
    cli = client.get("/api/v1/capabilities").json()["params_by_task"]["cli"]
    for spec in cli.values():
        cwd = next(p for p in spec["params"] if p["name"] == "cwd")
        assert cwd["type"] == "select"
        assert "options" in cwd


def test_server_block_names_variables_never_values(client):
    caps = client.get("/api/v1/capabilities").json()
    for row in caps["server"]:
        assert row["env_var"].isupper()
        # Values shown are derived facts (a count, a duration), not secrets or
        # filesystem paths.
        assert "/" not in row["value"]


def test_anthropic_max_tokens_is_marked_required(client):
    """One pair is not enough for a key: max_tokens is mandatory here and
    optional at the OpenAI-shaped providers."""
    chat = client.get("/api/v1/capabilities").json()["params_by_task"]["text-to-text"]
    override = chat["anthropic-api"]["model_overrides"]["max_tokens"]
    assert override["required"] is True
    assert override["default"] == capabilities._model_overrides("anthropic-api")[
        "max_tokens"
    ]["default"]


def test_glossary_is_served_from_one_place(client):
    labels = client.get("/api/v1/capabilities").json()["labels"]
    # All six price states have to be nameable, including the two that read as
    # zero if nobody spells them out.
    assert {"unpriced", "cost_unknown", "provider_notional"} <= set(labels["cost_sources"])
    assert labels["finish_reasons"]["length"]["label"] == "обрыв по лимиту токенов"
    assert labels["fallback_choices"][0]["value"] == "off"


class _Undeclared(ProviderAdapter):
    name = "x"
    supported_tasks = {Task.TEXT_TO_TEXT}

    def is_configured(self) -> bool:
        return True

    async def generate(self, request, model):  # pragma: no cover - never called
        raise NotImplementedError


def test_ignored_params_stays_quiet_when_nothing_was_declared():
    """Better silent than inventing a warning about an adapter we know nothing
    about."""
    assert ignored_params(_Undeclared(), Task.TEXT_TO_TEXT, {"seed": 1}) == []
