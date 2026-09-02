"""The `local` provider, driven with a stand-in model. No weights, no OpenCV.

What is checked here is the contract the router relies on rather than any
picture: a run's answer is an attachment priced at exactly zero, text with
nowhere to go is reported and not refused, roles and the row's allowlist bind
the way they do for Replicate, a third run waits for a slot while the event
loop stays free, a deadline is a retriable `timeout` that keeps its slot, and
missing weights are `model_not_loaded` rather than a 500.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from router_mcp.accounting.cost import compute_cost
from router_mcp.adapters.base import ignored_params
from router_mcp.adapters.local import LocalAdapter, LocalModel
from router_mcp.domain.errors import (
    BadRequest,
    ComputeTimeout,
    ModelNotLoaded,
    ModelUnavailable,
    ProviderError,
)
from router_mcp.domain.messages import Attachment, RouterRequest
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

WEIGHTS = ("fake/weights.bin",)


def _echo(handle, files, params):
    return b"OUT:" + files["image"], "image/png", {"params": params, "handle": handle}


def _spec(infer=_echo, params=(), inputs=("image",), loads: list | None = None) -> LocalModel:
    def load(models_dir: Path, threads: int):
        if loads is not None:
            loads.append(threads)
        return {"dir": str(models_dir), "threads": threads}

    return LocalModel(
        model_id="local/fake", display_name="Fake", task=Task.IMAGE_TO_IMAGE,
        inputs=inputs, weight_files=WEIGHTS, load=load, infer=infer, params=params,
    )


def _install(root: Path) -> None:
    for name in WEIGHTS:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"\x00")


def _adapter(root: Path, spec: LocalModel | None = None, **kw) -> LocalAdapter:
    spec = spec or _spec()
    return LocalAdapter(root, models={spec.model_id: spec}, **kw)


def _row(**kw) -> RegistryModel:
    return RegistryModel(
        service_provider_id="local", model_provider="local", model_id="local/fake",
        display_name="Fake", tasks=(Task.IMAGE_TO_IMAGE,), tier="free",
        image_price=0.0, price_source_kind="manual", **kw,
    )


def _request(**kw) -> RouterRequest:
    kw.setdefault(
        "attachments", [Attachment(kind="image", mime="image/png", data=b"PNG")]
    )
    return RouterRequest(task=Task.IMAGE_TO_IMAGE, external_user_id="t", **kw)


# --- the answer ---------------------------------------------------------------
async def test_the_result_is_an_attachment_and_the_run_costs_exactly_zero(tmp_path):
    _install(tmp_path)
    adapter = _adapter(tmp_path)
    response = await adapter.generate(_request(), _row())

    assert response.provider == "local"
    assert response.attachments[0].data == b"OUT:PNG"
    assert response.attachments[0].mime == "image/png"
    assert response.attachments[0].kind == "image"
    assert response.usage.images == 1
    assert response.usage.compute_seconds is not None
    assert response.raw_meta["applied_input"] == {"image": "image/png"}
    assert response.raw_meta["dropped_params"] == []

    # The accounting contract: a confirmed zero is «бесплатно», never «нет
    # методики». The spend panel reads image_price and must show free.
    cost = compute_cost(_row(), Modality.IMAGE, response.usage)
    assert cost.cost_usd == 0.0
    assert cost.cost_source == "computed"
    assert _row().price_is_unknown is False
    assert _row().is_free is True


async def test_text_the_task_only_offered_is_reported_not_refused(tmp_path):
    _install(tmp_path)
    adapter = _adapter(tmp_path)
    response = await adapter.generate(_request(text="выпрями страницу"), _row())
    assert response.raw_meta["dropped_params"] == ["prompt"]


async def test_the_model_loads_once_and_gets_its_share_of_the_cores(tmp_path):
    _install(tmp_path)
    loads: list[int] = []
    adapter = _adapter(tmp_path, _spec(loads=loads), max_concurrency=2)
    await adapter.generate(_request(), _row())
    await adapter.generate(_request(), _row())
    assert len(loads) == 1
    assert loads[0] == adapter._threads >= 1


# --- inputs -------------------------------------------------------------------
async def test_a_role_binds_through_the_rows_input_map(tmp_path):
    _install(tmp_path)
    spec = _spec(inputs=("page",))
    adapter = _adapter(tmp_path, spec)
    seen: dict = {}

    def infer(handle, files, params):
        seen.update(files)
        return b"x", "image/png", {}

    adapter = _adapter(tmp_path, _spec(infer=infer, inputs=("page",)))
    request = _request(
        attachments=[Attachment(kind="image", mime="image/jpeg", data=b"J", role="image")]
    )
    response = await adapter.generate(request, _row(input_map={"image": "page"}))
    assert seen == {"page": b"J"}
    assert response.raw_meta["applied_input"] == {"page": "image/jpeg, role=image"}


async def test_a_role_the_model_has_no_field_for_names_the_real_fields(tmp_path):
    _install(tmp_path)
    adapter = _adapter(tmp_path)
    request = _request(
        attachments=[
            Attachment(kind="image", mime="image/png", data=b"P"),
            Attachment(kind="image", mime="image/png", data=b"M", role="mask"),
        ]
    )
    with pytest.raises(ProviderError) as info:
        await adapter.generate(request, _row())
    assert "'mask'" in str(info.value)
    assert "its inputs are: image" in str(info.value)


async def test_a_loose_attachment_binds_positionally_and_a_spare_one_is_counted(tmp_path):
    _install(tmp_path)
    adapter = _adapter(tmp_path)
    request = _request(
        attachments=[
            Attachment(kind="image", mime="image/png", data=b"FIRST"),
            Attachment(kind="image", mime="image/png", data=b"SECOND"),
        ]
    )
    response = await adapter.generate(request, _row())
    assert response.attachments[0].data == b"OUT:FIRST"
    assert response.raw_meta["applied_input"]["unbound_attachments"] == 1


async def test_params_pass_only_when_the_row_allows_and_the_model_knows(tmp_path):
    _install(tmp_path)
    adapter = _adapter(tmp_path, _spec(params=("scale",)))
    row = _row(extra_params=("scale", "foo"))
    request = _request(params={"scale": 2, "foo": 1, "bar": 3})
    response = await adapter.generate(request, row)
    # `scale`: allowed by the row and known to the model — reaches it.
    assert response.raw_meta["local"]["params"] == {"scale": 2}
    # `foo`: allowed by the row, unknown to the model — dropped and named.
    assert response.raw_meta["dropped_params"] == ["foo"]
    # `bar`: not even allowed — the router's own ignored list catches it.
    assert ignored_params(adapter, Task.IMAGE_TO_IMAGE, request.params, row) == ["bar"]


async def test_a_url_only_attachment_is_refused_not_fetched(tmp_path):
    _install(tmp_path)
    adapter = _adapter(tmp_path)
    request = _request(
        attachments=[Attachment(kind="image", mime="image/png", url="https://x/y.png")]
    )
    with pytest.raises(BadRequest):
        await adapter.generate(request, _row())


async def test_a_ref_without_bytes_goes_through_the_injected_loader(tmp_path):
    _install(tmp_path)
    adapter = _adapter(tmp_path, load_blob=lambda ref: b"FROM:" + ref.encode())
    request = _request(attachments=[Attachment(kind="image", mime="image/png", ref="a.png")])
    response = await adapter.generate(request, _row())
    assert response.attachments[0].data == b"OUT:FROM:a.png"


# --- failures -------------------------------------------------------------------
async def test_missing_weights_are_an_installation_error_not_a_500(tmp_path):
    adapter = _adapter(tmp_path)  # nothing installed under tmp_path
    with pytest.raises(ModelNotLoaded) as info:
        await adapter.generate(_request(), _row())
    exc = info.value
    assert exc.code == "model_not_loaded"
    assert exc.http_status == 503
    assert exc.retriable is False
    assert "fake/weights.bin" in str(exc)
    assert exc.hint["kind"] == "fetch_models"


async def test_an_unknown_model_id_is_model_unavailable(tmp_path):
    adapter = _adapter(tmp_path)
    row = _row()
    row.model_id = "local/other"
    with pytest.raises(ModelUnavailable):
        await adapter.generate(_request(), row)


async def test_a_crash_inside_the_model_is_a_provider_error(tmp_path):
    _install(tmp_path)

    def broken(handle, files, params):
        raise ValueError("boom")

    adapter = _adapter(tmp_path, _spec(infer=broken))
    with pytest.raises(ProviderError) as info:
        await adapter.generate(_request(), _row())
    assert "ValueError: boom" in str(info.value)


# --- concurrency ------------------------------------------------------------------
async def test_the_third_run_waits_for_a_slot_and_the_loop_keeps_answering(tmp_path):
    _install(tmp_path)
    gate = threading.Event()
    started: list[float] = []
    lock = threading.Lock()

    def slow(handle, files, params):
        with lock:
            started.append(time.monotonic())
        gate.wait(5)
        return b"x", "image/png", {}

    adapter = _adapter(tmp_path, _spec(infer=slow), max_concurrency=2, timeout_seconds=10)
    runs = [asyncio.create_task(adapter.generate(_request(), _row())) for _ in range(3)]
    try:
        deadline = time.monotonic() + 3
        while len(started) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert len(started) == 2
        # The third is queued, not computing — and this loop is not blocked:
        # a sleep of 10 ms takes about 10 ms, not the seconds the workers hold.
        t = time.monotonic()
        await asyncio.sleep(0.05)
        assert time.monotonic() - t < 0.5
        assert len(started) == 2
        assert not any(r.done() for r in runs)
    finally:
        gate.set()
    results = await asyncio.gather(*runs)
    assert len(started) == 3
    assert all(r.attachments[0].data == b"x" for r in results)


async def test_a_run_past_the_deadline_is_a_retriable_timeout_that_keeps_its_slot(tmp_path):
    _install(tmp_path)
    gate = threading.Event()

    def stuck(handle, files, params):
        gate.wait(5)
        return b"late", "image/png", {}

    adapter = _adapter(tmp_path, _spec(infer=stuck), max_concurrency=2, timeout_seconds=0.05)
    with pytest.raises(ComputeTimeout) as info:
        await adapter.generate(_request(), _row())
    assert info.value.code == "timeout"
    assert info.value.retriable is True
    assert info.value.http_status == 504
    # The thread is still running, so its slot is still taken.
    assert adapter._slots._value == 1
    gate.set()
    deadline = time.monotonic() + 3
    while adapter._slots._value != 2 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert adapter._slots._value == 2
    await adapter.close()
