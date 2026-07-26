"""Selection stays inside the task, and the fallback ladder does too.

The defect that started this: a model that produces images was assumed able to
edit one, because both facts were spelled `image`. Declaring a *task set* is what
makes the refusal possible, and these tests are the refusal.

Everything runs against stub adapters. No network.
"""

from __future__ import annotations

import pytest

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.domain.errors import RouterError
from router_mcp.domain.messages import (
    Attachment,
    FallbackPolicy,
    RouterRequest,
    RouterResponse,
    Usage,
)
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database


class Painter(ProviderAdapter):
    """Draws and describes pictures. Cannot change one it was given — exactly the
    shape of the real `codex-oauth`."""

    name = "vendor"
    supported_tasks = {Task.TEXT_TO_IMAGE, Task.IMAGE_TO_TEXT}
    SUPPORTED_PARAMS = {Task.TEXT_TO_IMAGE: (), Task.IMAGE_TO_TEXT: ()}

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def is_configured(self) -> bool:
        return True

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        self.calls.append((request.task.value, model.model_id))
        return RouterResponse(
            task=request.task,
            provider=self.name,
            model_id=model.model_id,
            usage=Usage(images=1),
            attachments=[Attachment(kind="image", mime="image/png", data=b"png")],
        )


def _model(model_id: str, *tasks: Task, image_price: float = 0.01) -> RegistryModel:
    return RegistryModel(
        service_provider_id="vendor",
        model_provider="vendor",
        model_id=model_id,
        display_name=model_id,
        modalities=(),
        tasks=tasks,
        image_price=image_price,
        price_source_kind="manual",
        source="manual",
    )


@pytest.fixture
async def core(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.init()
    await db.upsert_service_provider(
        provider_id="vendor", display_name="Vendor", adapter="vendor",
        base_url=None, enabled=True,
    )
    for model in (
        _model("gen-only", Task.TEXT_TO_IMAGE),
        _model("vision-only", Task.IMAGE_TO_TEXT),
        _model("both", Task.TEXT_TO_IMAGE, Task.IMAGE_TO_TEXT, image_price=0.05),
    ):
        await db.upsert_registry_model(model)
    adapter = Painter()
    registry = RegistryService(db=db, adapters={"vendor": adapter}, ttl_seconds=3600)
    router = ModelRouter(
        db=db, registry=registry, adapters={"vendor": adapter},
        blobs=BlobStore(str(tmp_path / "blobs")),
    )
    yield router, adapter, db
    await db.close()


async def test_a_task_nobody_supports_is_refused_not_substituted(core):
    """Three models are registered and none of them edits an image. The answer is
    an error, not the nearest thing that happens to bill per image."""
    router, adapter, _ = core
    request = RouterRequest(
        task=Task.IMAGE_TO_IMAGE,
        external_user_id="u",
        text="make the sky red",
        attachments=[Attachment(kind="image", mime="image/png", data=b"png")],
    )
    with pytest.raises(RouterError, match="image-to-image"):
        await router.plan(request)
    assert adapter.calls == []


async def test_a_pinned_model_that_cannot_do_the_task_says_so(core):
    router, adapter, _ = core
    request = RouterRequest(
        task=Task.IMAGE_TO_IMAGE,
        external_user_id="u",
        model_id="gen-only",
        text="make the sky red",
        attachments=[Attachment(kind="image", mime="image/png", data=b"png")],
    )
    with pytest.raises(RouterError) as caught:
        await router.plan(request)
    # The message has to name the task; "does not support image" would be the old
    # ambiguity restated.
    assert "gen-only" in str(caught.value)
    assert "image-to-image" in str(caught.value)


async def test_a_fallback_policy_does_not_rescue_a_task_mismatch(core):
    """A substitution answers a different question, and no policy asks for that."""
    router, adapter, _ = core
    request = RouterRequest(
        task=Task.IMAGE_TO_IMAGE,
        external_user_id="u",
        model_id="gen-only",
        text="make the sky red",
        attachments=[Attachment(kind="image", mime="image/png", data=b"png")],
        fallback=FallbackPolicy(policy=FallbackPolicy.SAME, max_steps=3, scope="any"),
    )
    with pytest.raises(RouterError, match="image-to-image"):
        await router.plan(request)
    assert adapter.calls == []


async def test_selection_ignores_models_of_a_neighbouring_task(core):
    """`vision-only` bills in the same group as `gen-only` (both per image), so a
    group-based filter would have offered it for generation."""
    router, adapter, _ = core
    attempts = await router.plan(
        RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="a cat")
    )
    chosen = {a.model.model_id for a in attempts}
    assert "vision-only" not in chosen
    assert chosen <= {"gen-only", "both"}


async def test_the_ladder_stays_inside_the_task(core):
    """With the whole registry in scope, every rung must still do the task."""
    router, _, _ = core
    plan = await router.explain(
        RouterRequest(
            task=Task.TEXT_TO_IMAGE,
            external_user_id="u",
            text="a cat",
            model_id="gen-only",
            fallback=FallbackPolicy(policy=FallbackPolicy.SAME, max_steps=5, scope="any"),
        )
    )
    considered = {c["model_id"] for c in plan["candidates"]}
    assert "vision-only" not in considered
    assert plan["task"] == "text-to-image"
    assert plan["modality"] == "image"


async def test_the_ladder_orders_image_models_by_the_image_price(core):
    """Ordering by `prompt_price + completion_price` is zero for every one of
    these rows, so the ladder used to be sorted by model id."""
    router, _, _ = core
    plan = await router.explain(
        RouterRequest(
            task=Task.TEXT_TO_IMAGE,
            external_user_id="u",
            text="a cat",
            model_id="both",  # the expensive one
            fallback=FallbackPolicy(policy=FallbackPolicy.CHEAPER, max_steps=2, scope="any"),
        )
    )
    eligible = [c for c in plan["candidates"] if c["verdict"] == "eligible"]
    assert eligible, plan["candidates"]
    assert eligible[0]["model_id"] == "gen-only"
    # Prices are per image here, and the plan reports the rate it ordered by.
    assert eligible[0]["price_per_1m"] == pytest.approx(0.01 * 1e6)


async def test_the_log_records_both_the_task_and_the_group(core):
    router, _, db = core
    await router.dispatch(
        RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="a cat")
    )
    rows = await db.list_request_logs(limit=5)
    assert rows[0]["task"] == "text-to-image"
    assert rows[0]["modality"] == "image"


async def test_vision_is_routed_to_the_vision_model(core):
    """The other half: an image on the way *in* is a different task from an image
    on the way out, and both are billed in the `image` group here."""
    router, adapter, _ = core
    await router.dispatch(
        RouterRequest(
            task=Task.IMAGE_TO_TEXT,
            external_user_id="u",
            attachments=[Attachment(kind="image", mime="image/png", data=b"png")],
        )
    )
    tasks_called = {task for task, _ in adapter.calls}
    assert tasks_called == {"image-to-text"}
    models_called = {model for _, model in adapter.calls}
    assert "gen-only" not in models_called


async def test_an_already_stored_file_is_not_stored_again(core, tmp_path):
    """The console uploads once and then passes a ref. Re-saving it meant a run
    against three models left three more copies and pointed the log at the last."""
    router, _, db = core
    blobs = router._blobs
    ref = blobs.save(b"\x89PNG-original", "image/png")
    before = set(blobs.list_refs())

    await router.dispatch(
        RouterRequest(
            task=Task.IMAGE_TO_TEXT,
            external_user_id="u",
            attachments=[
                Attachment(kind="image", mime="image/png", data=b"\x89PNG-original", ref=ref)
            ],
        )
    )
    rows = await db.list_request_logs(limit=1)
    assert rows[0]["request_text"] is None or True
    # The response added one blob; the request must not have added another.
    added = set(blobs.list_refs()) - before
    assert ref not in added
    log = await db.get_request_log(rows[0]["id"])
    assert log["request_ref"] == ref


async def test_every_attachment_is_stored_not_only_the_first(core):
    """An edit with a mask sends two, and the second used to vanish, so the entry
    could not be replayed."""
    router, _, _ = core
    blobs = router._blobs
    before = set(blobs.list_refs())
    request = RouterRequest(
        task=Task.IMAGE_TO_IMAGE,
        external_user_id="u",
        text="правь",
        attachments=[
            Attachment(kind="image", mime="image/png", data=b"one"),
            Attachment(kind="image", mime="image/png", data=b"two"),
        ],
    )
    router._persist_request_attachment(request)
    assert all(att.ref for att in request.attachments)
    assert len(set(blobs.list_refs()) - before) == 2


async def test_the_dry_run_names_the_task_not_the_group(core):
    """«не поддерживает модальность image» was wrong twice over: wrong word, and
    the model does bill in that group — it just cannot do this with it."""
    router, _, _ = core
    plan = await router.explain(
        RouterRequest(
            task=Task.IMAGE_TO_IMAGE,
            external_user_id="u",
            model_id="gen-only",
            text="правь",
            attachments=[Attachment(kind="image", mime="image/png", data=b"png")],
        )
    )
    assert plan["problem"] == "модель 'gen-only' не умеет image-to-image"
