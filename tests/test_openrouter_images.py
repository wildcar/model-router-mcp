"""OpenRouter's dedicated images endpoint. No network.

Some image models answer only on POST /images and are absent from GET /models
entirely (meta/muse-image was the first — its chat call came back
«…cannot be used with the chat/completions endpoint», seen live on prod, log
16434). The contract under test: a row marked `images_api` goes straight
there, an unmarked row follows the provider's own 404 hint, references ride
in `input_references`, and the base64 answer becomes attachments with the
usage and the provider's cost kept.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from router_mcp.adapters.openrouter import OpenRouterAdapter
from router_mcp.domain.errors import ModelUnavailable, ProviderError
from router_mcp.domain.messages import Attachment, RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

BASE = "https://openrouter.test/api/v1"
PNG = b"\x89PNG fake bytes"

CHAT_REFUSAL = (
    '{"error":{"message":"meta/muse-image is an image generation model and '
    "cannot be used with the chat/completions endpoint. "
    'Use the /api/v1/images endpoint instead.","code":404}}'
)


def _images_body(n: int = 1) -> dict[str, Any]:
    return {
        "id": "img-1",
        "data": [
            {"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}
            for _ in range(n)
        ],
        "usage": {"cost": 0.011, "completion_tokens": 4321},
    }


def _adapter(responses: dict[str, httpx.Response]):
    """An adapter whose transport answers by path and keeps every request."""
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[request.url.path.rsplit("/", 1)[-1]]

    adapter = OpenRouterAdapter(api_key="sk-test", base_url=BASE, retries=0)
    adapter._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(handle)
    )
    return adapter, calls


def _model(capabilities: tuple[str, ...] = ("images_api",)) -> RegistryModel:
    return RegistryModel(
        service_provider_id="openrouter",
        model_provider="meta",
        model_id="meta/muse-image",
        display_name="Meta: Muse Image",
        modalities=(Modality.IMAGE,),
        tasks=(Task.TEXT_TO_IMAGE, Task.IMAGE_TO_IMAGE),
        capabilities=capabilities,
        source="manual",
        price_source_kind="estimate",
    )


@pytest.mark.asyncio
async def test_marked_row_goes_straight_to_images():
    adapter, calls = _adapter({"images": httpx.Response(200, json=_images_body())})
    request = RouterRequest(
        Task.TEXT_TO_IMAGE, "u", text="дерево на закате",
        params={"aspect_ratio": "1:1", "n": 1, "temperature": 0.7},
    )
    response = await adapter.generate(request, _model())

    assert [c.url.path for c in calls] == ["/api/v1/images"]
    payload = json.loads(calls[0].content.decode())
    assert payload["model"] == "meta/muse-image"
    assert payload["prompt"] == "дерево на закате"
    assert payload["aspect_ratio"] == "1:1" and payload["n"] == 1
    # A chat knob has no meaning on this endpoint and must not be sent on.
    assert "temperature" not in payload
    assert response.usage.images == 1
    assert response.attachments[0].data == PNG
    assert response.attachments[0].mime == "image/png"
    assert response.raw_meta["provider_cost_usd"] == 0.011
    assert response.raw_meta["endpoint"] == "/images"


@pytest.mark.asyncio
async def test_reference_images_ride_in_input_references():
    adapter, calls = _adapter({"images": httpx.Response(200, json=_images_body())})
    request = RouterRequest(
        Task.IMAGE_TO_IMAGE, "u", text="убери фон",
        attachments=[Attachment(kind="image", mime="image/jpeg", data=b"jpeg")],
    )
    await adapter.generate(request, _model())
    payload = json.loads(calls[0].content.decode())
    ref = payload["input_references"][0]
    assert ref["type"] == "image_url"
    assert ref["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_unmarked_row_follows_the_chat_404_hint():
    """The provider says which endpoint to use; that is routing advice, not a
    retired model, so the request must not die on it."""
    adapter, calls = _adapter({
        "completions": httpx.Response(404, text=CHAT_REFUSAL),
        "images": httpx.Response(200, json=_images_body()),
    })
    request = RouterRequest(Task.TEXT_TO_IMAGE, "u", text="дерево")
    response = await adapter.generate(request, _model(capabilities=()))
    assert [c.url.path for c in calls] == [
        "/api/v1/chat/completions", "/api/v1/images",
    ]
    assert response.usage.images == 1


@pytest.mark.asyncio
async def test_a_plain_404_still_means_the_model_is_gone():
    adapter, _calls = _adapter({
        "completions": httpx.Response(404, text='{"error":{"message":"No such model"}}'),
    })
    request = RouterRequest(Task.TEXT_TO_IMAGE, "u", text="дерево")
    with pytest.raises(ModelUnavailable):
        await adapter.generate(request, _model(capabilities=()))


@pytest.mark.asyncio
async def test_empty_answer_is_a_named_failure():
    adapter, _calls = _adapter({
        "images": httpx.Response(200, json={"data": [], "usage": {"cost": 0}}),
    })
    request = RouterRequest(Task.TEXT_TO_IMAGE, "u", text="дерево")
    with pytest.raises(ProviderError, match="empty response"):
        await adapter.generate(request, _model())


@pytest.mark.asyncio
async def test_images_path_carries_the_app_identity():
    adapter, calls = _adapter({"images": httpx.Response(200, json=_images_body())})
    request = RouterRequest(
        Task.TEXT_TO_IMAGE, "u", text="дерево",
        app_url="https://agentish.example", app_name="Agentish",
    )
    await adapter.generate(request, _model())
    assert calls[0].headers["HTTP-Referer"] == "https://agentish.example"
    assert calls[0].headers["X-Title"] == "Agentish"


# --- the booked cost ----------------------------------------------------------
# OpenRouter's image models bill by token (prompt and input photo at one rate,
# the output image at another), so one `image_price` per picture cannot fit
# both drawing and editing: on prod an edit on gpt-image-2.5 was booked at
# $0.005 against the provider's $0.016921 (log 26200). For image tasks the
# provider's `usage.cost` is the booked sum; a text task keeps the registry
# arithmetic, so the reconciliation filter can still catch a stale rate.

def _gpt_image() -> RegistryModel:
    return RegistryModel(
        service_provider_id="openrouter",
        model_provider="openai",
        model_id="openai/gpt-image-2.5-sunburst",
        display_name="OpenAI: GPT Image 2.5 Sunburst",
        modalities=(Modality.IMAGE,),
        tasks=(Task.TEXT_TO_IMAGE, Task.IMAGE_TO_IMAGE),
        capabilities=("images_api",),
        image_price=0.0066,
        source="manual",
        price_source_kind="manual",
    )


def _edit_body() -> dict[str, Any]:
    # The usage shape seen live on prod for an image-to-image run.
    body = _images_body()
    body["usage"] = {
        "prompt_tokens": 1481, "completion_tokens": 172, "total_tokens": 1653,
        "completion_tokens_details": {"image_tokens": 172},
        "cost": 0.016921,
    }
    return body


@pytest.mark.asyncio
async def test_images_path_books_the_provider_sum():
    adapter, _calls = _adapter({"images": httpx.Response(200, json=_edit_body())})
    request = RouterRequest(
        Task.IMAGE_TO_IMAGE, "u", text="сделай закат",
        attachments=[Attachment(kind="image", mime="image/png", data=PNG)],
    )
    response = await adapter.generate(request, _gpt_image())
    assert response.cost_override_usd == pytest.approx(0.016921)
    assert response.cost_is_notional is False
    # Still kept in the meta: the inspector prints it beside the booked sum.
    assert response.raw_meta["provider_cost_usd"] == pytest.approx(0.016921)


@pytest.mark.asyncio
async def test_images_path_without_a_provider_sum_falls_back_to_the_registry_rate():
    body = _images_body()
    body["usage"] = {"completion_tokens": 215}
    adapter, _calls = _adapter({"images": httpx.Response(200, json=body)})
    response = await adapter.generate(
        RouterRequest(Task.TEXT_TO_IMAGE, "u", text="маяк"), _gpt_image()
    )
    assert response.cost_override_usd is None
    assert response.raw_meta["provider_cost_usd"] is None


@pytest.mark.asyncio
async def test_chat_path_image_task_books_the_provider_sum():
    """A chat-shaped image model (the Gemini image family) is billed the same
    way; the endpoint it answered on must not decide how the money is booked."""
    chat_body = {
        "id": "gen-2",
        "choices": [{
            "message": {
                "content": "",
                "images": [{"image_url": {"url": "data:image/png;base64,AAAA"}}],
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 26, "completion_tokens": 215, "cost": 0.0067},
    }
    adapter, calls = _adapter({"completions": httpx.Response(200, json=chat_body)})
    response = await adapter.generate(
        RouterRequest(Task.TEXT_TO_IMAGE, "u", text="маяк"), _model(capabilities=())
    )
    assert [c.url.path for c in calls] == ["/api/v1/chat/completions"]
    assert response.usage.images == 1
    assert response.cost_override_usd == pytest.approx(0.0067)


@pytest.mark.asyncio
async def test_a_text_task_keeps_the_registry_arithmetic():
    chat_body = {
        "id": "gen-3",
        "choices": [{"message": {"content": "привет"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.028},
    }
    adapter, _calls = _adapter({"completions": httpx.Response(200, json=chat_body)})
    text_model = RegistryModel(
        service_provider_id="openrouter", model_provider="openai",
        model_id="openai/gpt-4o", display_name="gpt-4o",
        modalities=(Modality.CHAT,), prompt_price=1e-7, completion_price=1e-7,
        source="manual", price_source_kind="manual",
    )
    response = await adapter.generate(
        RouterRequest(Task.TEXT_TO_TEXT, "u", text="эй"), text_model
    )
    assert response.cost_override_usd is None
    assert response.raw_meta["provider_cost_usd"] == pytest.approx(0.028)


@pytest.mark.asyncio
async def test_the_journal_books_the_provider_sum_for_an_edit(tmp_path):
    """End to end: the row's cost_usd equals usage.cost, cost_source says
    `provider`, and the reconciliation filter no longer flags the run."""
    from router_mcp.blobs.store import BlobStore
    from router_mcp.registry.service import RegistryService
    from router_mcp.routing.router import ModelRouter
    from router_mcp.storage.database import Database

    adapter, _calls = _adapter({"images": httpx.Response(200, json=_edit_body())})
    db = Database(str(tmp_path / "img.db"))
    await db.init()
    await db.upsert_service_provider("openrouter", "OpenRouter", "openrouter", None, enabled=True)
    await db.upsert_registry_model(_gpt_image())
    router = ModelRouter(
        db, RegistryService(db, adapters={}), {"openrouter": adapter},
        BlobStore(str(tmp_path / "blobs")),
    )
    try:
        result = await router.dispatch(
            RouterRequest(
                Task.IMAGE_TO_IMAGE, "agentish-7",
                model_id="openai/gpt-image-2.5-sunburst", text="сделай закат",
                attachments=[Attachment(kind="image", mime="image/png", data=PNG)],
            )
        )
        assert result.cost.cost_source == "provider"
        assert result.cost.cost_usd == pytest.approx(0.016921)

        row = await db.get_request_log(result.log_id)
        assert row is not None
        assert row["cost_usd"] == pytest.approx(0.016921)
        assert row["cost_source"] == "provider"
        assert row["provider_cost_usd"] == pytest.approx(0.016921)
        flagged = await db.list_request_logs(cost_mismatch=True)
        assert [r["id"] for r in flagged] == []
    finally:
        await db.close()
