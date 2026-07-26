"""The Replicate adapter, driven against a fake transport. No network.

Three of these are about money rather than correctness, and they are the reason
the file exists: a retried create bills twice, a cancelled run keeps billing until
somebody says stop, and an output that is only a link is gone in an hour.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from router_mcp.adapters.replicate import (
    ReplicateAdapter,
    infer_task,
    parse_models,
)
from router_mcp.domain import progress
from router_mcp.domain.errors import ProviderError, ProviderTimeout
from router_mcp.domain.messages import Attachment, RouterRequest, Usage
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

FLUX_SCHEMA = {
    "properties": {
        "prompt": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": ["1:1", "16:9"]},
        "num_outputs": {"type": "integer", "minimum": 1, "maximum": 4},
        "seed": {"type": "integer"},
    },
    "required": ["prompt"],
}
KONTEXT_SCHEMA = {
    "properties": {
        "prompt": {"type": "string"},
        "input_image": {"type": "string", "format": "uri"},
    },
    "required": ["prompt", "input_image"],
}


def _adapter(handler=None, **kw) -> ReplicateAdapter:
    adapter = ReplicateAdapter(
        api_token="r8_test", base_url="https://api.replicate.com/v1",
        models="black-forest-labs/flux-schnell:text-to-image", **kw,
    )
    if handler is not None:
        transport = httpx.MockTransport(handler)
        adapter._client = httpx.AsyncClient(
            base_url="https://api.replicate.com/v1", transport=transport,
            headers={"Authorization": "Bearer r8_test"},
        )
        adapter._files = httpx.AsyncClient(transport=transport, follow_redirects=True)
    return adapter


def _info(adapter, slug="black-forest-labs/flux-schnell", schema=FLUX_SCHEMA, **kw):
    import time as _time

    from router_mcp.adapters.replicate import ModelInfo

    kw.setdefault("version_id", "v1")
    # Fresh, so `generate()` uses the cache instead of fetching the document.
    info = ModelInfo(slug=slug, input_schema=schema, fetched_at=_time.monotonic(), **kw)
    adapter._infos[slug] = info
    return info


def _model(slug="black-forest-labs/flux-schnell") -> RegistryModel:
    return RegistryModel(
        service_provider_id="replicate", model_provider="black-forest-labs",
        model_id=slug, display_name=slug, tasks=(Task.TEXT_TO_IMAGE,),
    )


# --- output shapes ---------------------------------------------------------
def test_a_single_url_is_a_file_not_text():
    text, urls = ReplicateAdapter._collect_outputs(
        "https://replicate.delivery/x/out.png"
    )
    assert text == ""
    assert urls == ["https://replicate.delivery/x/out.png"]


def test_a_list_of_urls_is_all_of_them():
    out = ["https://replicate.delivery/a.png", "https://replicate.delivery/b.png"]
    text, urls = ReplicateAdapter._collect_outputs(out)
    assert (text, urls) == ("", out)


def test_a_string_is_text():
    text, urls = ReplicateAdapter._collect_outputs("a long paragraph")
    assert (text, urls) == ("a long paragraph", [])


def test_streamed_token_pieces_are_joined_with_nothing():
    """The regression that matters: these are token fragments, and joining them
    with a newline mangles every LLM answer this provider gives."""
    text, urls = ReplicateAdapter._collect_outputs(["Hello", ",", " ", "world"])
    assert text == "Hello, world"
    assert urls == []


def test_a_dict_output_is_walked():
    text, urls = ReplicateAdapter._collect_outputs(
        {"video": "https://replicate.delivery/c.mp4", "caption": "a cat"}
    )
    assert urls == ["https://replicate.delivery/c.mp4"]
    assert text == "a cat"


def test_nothing_at_all_is_reported_as_nothing():
    assert ReplicateAdapter._collect_outputs(None) == ("", [])
    assert ReplicateAdapter._collect_outputs([]) == ("", [])


# --- building the input ----------------------------------------------------
async def test_the_prompt_lands_on_the_property_the_model_actually_has():
    adapter = _adapter()
    info = _info(adapter)
    payload, dropped = await adapter._build_input(
        RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот"), info
    )
    assert payload == {"prompt": "кот"}
    assert dropped == []


async def test_an_image_lands_on_this_models_own_image_property():
    """`image`, `input_image`, `img`... every model names it differently, which is
    why the schema is read instead of guessed."""
    adapter = _adapter()
    info = _info(adapter, slug="black-forest-labs/flux-kontext-pro", schema=KONTEXT_SCHEMA)
    payload, _ = await adapter._build_input(
        RouterRequest(
            task=Task.IMAGE_TO_IMAGE, external_user_id="u", text="сделай небо красным",
            attachments=[Attachment(kind="image", mime="image/png", data=b"\x89PNG")],
        ),
        info,
    )
    assert payload["prompt"] == "сделай небо красным"
    assert payload["input_image"].startswith("data:image/png;base64,")


async def test_a_declared_param_this_model_lacks_is_dropped_and_named():
    adapter = _adapter()
    info = _info(adapter)
    payload, dropped = await adapter._build_input(
        RouterRequest(
            task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот",
            params={"seed": 42, "guidance_scale": 7.5},
        ),
        info,
    )
    assert payload["seed"] == 42
    # flux-schnell has no guidance_scale, so it is reported rather than sent.
    assert dropped == ["guidance_scale"]
    assert "guidance_scale" not in payload


async def test_an_undeclared_param_never_reaches_the_payload():
    """`SUPPORTED_PARAMS` is the gate: a caller cannot set `webhook` or spend the
    operator's money on `num_outputs: 50` through a passthrough."""
    adapter = _adapter()
    info = _info(adapter)
    payload, _ = await adapter._build_input(
        RouterRequest(
            task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот",
            params={"webhook": "https://evil.example"},
        ),
        info,
    )
    assert "webhook" not in payload


async def test_a_value_outside_the_schema_fails_here_not_at_the_provider():
    adapter = _adapter()
    info = _info(adapter)
    with pytest.raises(ProviderError, match="1:1"):
        await adapter._build_input(
            RouterRequest(
                task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот",
                params={"aspect_ratio": "3:2"},
            ),
            info,
        )
    with pytest.raises(ProviderError, match="maximum"):
        await adapter._build_input(
            RouterRequest(
                task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот",
                params={"num_outputs": 50},
            ),
            info,
        )


async def test_a_model_with_no_prompt_property_names_its_real_inputs():
    """The whole reason to read the schema: the alternative is a 422 the operator
    cannot act on."""
    adapter = _adapter()
    info = _info(adapter, schema={"properties": {"image": {}, "mask": {}, "seed": {}}})
    with pytest.raises(ProviderError, match="image, mask, seed"):
        await adapter._build_input(
            RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот"), info
        )


async def test_a_big_file_says_how_to_hand_it_over():
    adapter = _adapter()
    info = _info(adapter, slug="x/y", schema=KONTEXT_SCHEMA)
    with pytest.raises(ProviderError, match="PUBLIC_BLOB_BASE_URL"):
        await adapter._build_input(
            RouterRequest(
                task=Task.IMAGE_TO_IMAGE, external_user_id="u", text="правь",
                attachments=[
                    Attachment(kind="image", mime="image/png", data=b"x" * (300 * 1024))
                ],
            ),
            info,
        )


async def test_a_big_file_goes_by_public_link_when_one_is_configured():
    adapter = _adapter(public_blob_url=lambda ref: f"https://blob.example.org/p/{ref}")
    info = _info(adapter, slug="x/y", schema=KONTEXT_SCHEMA)
    payload, _ = await adapter._build_input(
        RouterRequest(
            task=Task.IMAGE_TO_IMAGE, external_user_id="u", text="правь",
            attachments=[
                Attachment(
                    kind="image", mime="image/png", data=b"x" * (300 * 1024), ref="r1.png"
                )
            ],
        ),
        info,
    )
    assert payload["input_image"] == "https://blob.example.org/p/r1.png"


# --- the call --------------------------------------------------------------
def _succeeded(output, **kw):
    return {
        "id": "pred-1", "status": "succeeded", "output": output,
        "metrics": {"predict_time": 2.5, "total_time": 4.0}, **kw,
    }


async def test_a_versioned_model_posts_to_predictions():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_succeeded("done"))

    adapter = _adapter(handler)
    info = _info(adapter)
    await adapter._create(info, {"prompt": "кот"})
    assert seen["url"].endswith("/predictions")
    assert seen["body"]["version"] == "v1"


async def test_an_official_model_posts_to_its_own_route():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_succeeded("done"))

    adapter = _adapter(handler)
    info = _info(adapter, slug="meta/musicgen", version_id=None)
    await adapter._create(info, {"prompt": "джаз"})
    assert seen["url"].endswith("/models/meta/musicgen/predictions")
    assert "version" not in seen["body"]


async def test_creating_is_not_retried_on_a_server_error():
    """A retried create makes a second billable prediction nobody collects."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500, text="boom")

    adapter = _adapter(handler)
    info = _info(adapter)
    with pytest.raises(Exception):
        await adapter._create(info, {"prompt": "кот"})
    assert len(calls) == 1


async def test_creating_is_retried_on_a_rate_limit(monkeypatch):
    """429 is the one failure that means nothing was created."""
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=_succeeded("done"))

    adapter = _adapter(handler, )
    info = _info(adapter)
    result = await adapter._create(info, {"prompt": "кот"})
    assert len(calls) == 2
    assert result["status"] == "succeeded"


async def _no_sleep(_seconds):
    return None


# --- polling ---------------------------------------------------------------
async def test_polling_runs_until_a_terminal_state(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    states = ["starting", "processing", "processing", "succeeded"]

    def handler(request: httpx.Request) -> httpx.Response:
        status = states.pop(0) if states else "succeeded"
        return httpx.Response(200, json={
            "id": "pred-1", "status": status, "output": "готово" if status == "succeeded" else None,
            "metrics": {"predict_time": 3.0},
        })

    adapter = _adapter(handler)
    final = await adapter._poll({"id": "pred-1", "status": "starting", "urls": {}})
    assert final["status"] == "succeeded"


async def test_a_long_run_does_not_flood_the_timeline(monkeypatch):
    """The router caps a run at 500 steps. Ten minutes of polling must not spend
    a tenth of that budget saying "still going"."""
    elapsed = {"t": 0.0}
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        "router_mcp.adapters.replicate.time.monotonic",
        lambda: elapsed.__setitem__("t", elapsed["t"] + 5) or elapsed["t"],
    )
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        polls["n"] += 1
        status = "succeeded" if polls["n"] > 120 else "processing"
        return httpx.Response(200, json={
            "id": "p", "status": status, "output": "ok" if status == "succeeded" else None,
            "logs": "45%|████ | 9/20", "metrics": {},
        })

    steps: list[dict] = []
    token = progress.set_sink(steps.append)
    try:
        adapter = _adapter(handler, poll_deadline_seconds=100000)
        await adapter._poll({"id": "p", "status": "starting", "urls": {}})
    finally:
        progress.reset_sink(token)
    assert polls["n"] > 100          # it really did poll many times
    assert len(steps) < 20, steps    # and said so a handful of times
    # The model's own progress line is the useful part of a heartbeat.
    assert any("45%" in str(s.get("log_tail")) for s in steps)


async def test_the_deadline_cancels_the_prediction(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "router_mcp.adapters.replicate.time.monotonic",
        lambda: clock.__setitem__("t", clock["t"] + 100) or clock["t"],
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"id": "p", "status": "processing", "metrics": {}})

    adapter = _adapter(handler, poll_deadline_seconds=10)
    with pytest.raises(ProviderTimeout):
        await adapter._poll({"id": "p", "status": "processing", "urls": {}})
    assert any(call.endswith("/predictions/p/cancel") for call in seen), seen


async def test_cancelling_a_run_stops_the_meter(monkeypatch):
    """Without this a cancelled video keeps billing until it finishes."""
    seen: list[str] = []
    started = asyncio.Event()
    real_sleep = asyncio.sleep

    async def slow_sleep(_seconds):
        """Park the poll loop so the test can cancel it mid-wait. Holds the real
        `sleep`, since the patched name is this function."""
        started.set()
        await real_sleep(3600)

    monkeypatch.setattr(asyncio, "sleep", slow_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"id": "p", "status": "processing"})

    adapter = _adapter(handler)
    task = asyncio.ensure_future(
        adapter._poll({"id": "p", "status": "processing", "urls": {}})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(path.endswith("/predictions/p/cancel") for path in seen), seen


async def test_a_stale_version_is_refetched_once(monkeypatch):
    """An author pushing new weights is the normal case, not an outage."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if request.method == "POST":
            if len([c for c in calls if c.startswith("POST")]) == 1:
                return httpx.Response(400, text="version does not exist")
            return httpx.Response(200, json=_succeeded(["https://replicate.delivery/a.png"]))
        return httpx.Response(200, json={
            "name": "flux-schnell",
            "latest_version": {"id": "v2", "openapi_schema": {
                "components": {"schemas": {"Input": FLUX_SCHEMA, "Output": {}}}
            }},
        })

    adapter = _adapter(handler)
    _info(adapter)
    response = await adapter.generate(
        RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот"),
        _model(),
    )
    assert response.raw_meta["version"] == "v2"
    assert sum(1 for c in calls if c.startswith("POST")) == 2


# --- results ---------------------------------------------------------------
async def test_a_result_is_fetched_into_bytes():
    """Replicate's links expire in about an hour, so an attachment that is only a
    URL means tomorrow's log entry points at nothing."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.replicate.com":
            return httpx.Response(
                200, content=b"\x89PNG-data", headers={"content-type": "image/png"}
            )
        return httpx.Response(200, json=_succeeded(["https://replicate.delivery/a.png"]))

    adapter = _adapter(handler)
    _info(adapter)
    response = await adapter.generate(
        RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот"), _model()
    )
    assert response.attachments[0].data == b"\x89PNG-data"
    assert response.attachments[0].mime == "image/png"
    assert response.usage.compute_seconds == 2.5
    assert response.usage.images == 1


async def test_the_download_client_carries_no_api_token():
    """Output files live on a different host, and httpx sends client-level
    headers to absolute URLs too."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.replicate.com":
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, content=b"x", headers={"content-type": "image/png"})
        return httpx.Response(200, json=_succeeded(["https://replicate.delivery/a.png"]))

    adapter = _adapter(handler)
    _info(adapter)
    await adapter.generate(
        RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот"), _model()
    )
    assert seen["auth"] is None


async def test_an_oversized_result_stays_a_link_and_the_run_still_succeeds():
    """The money is already spent; failing the run would destroy the only record
    of having spent it."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.replicate.com":
            return httpx.Response(
                200, content=b"x" * 1024,
                headers={"content-type": "video/mp4", "content-length": str(90 * 1024 * 1024)},
            )
        return httpx.Response(200, json=_succeeded(["https://replicate.delivery/a.mp4"]))

    adapter = _adapter(handler, max_output_mb=1.0)
    _info(adapter)
    response = await adapter.generate(
        RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот"), _model()
    )
    assert response.attachments[0].data is None
    assert response.attachments[0].url.endswith(".mp4")
    assert response.raw_meta["outputs_not_downloaded"]


async def test_a_failed_prediction_names_the_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "p", "status": "failed", "error": "CUDA out of memory",
            "logs": "trace line", "metrics": {},
        })

    adapter = _adapter(handler)
    _info(adapter)
    with pytest.raises(ProviderError, match="CUDA out of memory"):
        await adapter.generate(
            RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот"),
            _model(),
        )


async def test_an_empty_success_is_still_a_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_succeeded(None))

    adapter = _adapter(handler)
    _info(adapter)
    with pytest.raises(ProviderError, match="no output"):
        await adapter.generate(
            RouterRequest(task=Task.TEXT_TO_IMAGE, external_user_id="u", text="кот"),
            _model(),
        )


async def test_token_metrics_reach_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "p", "status": "succeeded", "output": ["Hello", " world"],
            "metrics": {"predict_time": 1.5, "input_token_count": 20,
                        "output_token_count": 5},
        })

    adapter = _adapter(handler)
    _info(adapter)
    response = await adapter.generate(
        RouterRequest(task=Task.TEXT_TO_TEXT, external_user_id="u", text="hi"), _model()
    )
    assert response.text == "Hello world"
    assert response.usage.prompt_tokens == 20
    assert response.usage.total_tokens == 25
    # No files came back, so nothing is counted as an image.
    assert response.usage.images is None


# --- catalog and price provenance ------------------------------------------
def test_the_allowlist_is_parsed_with_optional_tasks():
    assert parse_models("a/b, c/d:image-to-image , ") == [
        ("a/b", None), ("c/d", Task.IMAGE_TO_IMAGE)
    ]


async def test_an_empty_allowlist_reports_no_catalog_at_all():
    """`[]` would tell the sync to deprecate every Replicate row, including the
    ones an operator added by hand."""
    adapter = ReplicateAdapter(api_token="r8", base_url="https://x", models="")
    assert await adapter.list_catalog() is None
    await adapter.close()


async def test_a_synced_row_says_its_price_is_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "name": "flux-schnell",
            "latest_version": {"id": "v1", "openapi_schema": {
                "components": {"schemas": {"Input": FLUX_SCHEMA, "Output": {}}}
            }},
        })

    adapter = _adapter(handler)
    rows = await adapter.list_catalog()
    row = rows[0]
    assert row.price_source_kind == "unknown"
    assert row.price_source.startswith("page:https://replicate.com/")
    assert row.price_checked_at is None
    # Consequences that follow, and are the point of the provenance:
    assert row.price_is_unknown is True
    assert row.tier == "expensive"     # compute_tier(0, 0) would say "free"
    assert row.tasks == (Task.TEXT_TO_IMAGE,)
    assert row.modalities == (Modality.IMAGE,)


def test_the_task_is_inferred_when_the_operator_did_not_say():
    image_out = {"type": "array", "items": {"type": "string", "format": "uri"}}
    assert infer_task(FLUX_SCHEMA, image_out, {}) is Task.TEXT_TO_IMAGE
    assert infer_task(KONTEXT_SCHEMA, image_out, {}) is Task.IMAGE_TO_IMAGE
    assert infer_task(
        KONTEXT_SCHEMA, image_out, {"description": "generate a video clip"}
    ) is Task.IMAGE_TO_VIDEO
    assert infer_task({"properties": {"prompt": {}}}, {"type": "string"}, {}) is Task.TEXT_TO_TEXT
