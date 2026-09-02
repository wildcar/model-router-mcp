"""Acceptance for the `local` provider over HTTP: the whole path a client
walks. `blobs` → `runs` → poll → the result as a blob → a `usage` row at zero.

Skips itself without the runtime libraries or the weights; on the host that
serves the model it runs against the real UVDoc.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from router_mcp.adapters import local_uvdoc
from router_mcp.runtime import get_core


def _ready(client) -> None:
    adapter = get_core().adapters["local"]
    if not adapter.is_configured():
        pytest.skip("local runtime libraries not installed (pip install -e '.[local]')")
    root = Path(get_core().settings.local_models_dir)
    if not all((root / f).is_file() for f in local_uvdoc.WEIGHT_FILES):
        pytest.skip("UVDoc weights not installed (deploy/fetch-models.sh)")


def _wait(client, run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/runs/{run_id}").json()
        if state["status"] in ("ok", "error", "cancelled"):
            return state
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish in {timeout}s")


def _photo_png() -> bytes:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    page = np.full((600, 480, 3), 235, np.uint8)
    for y in range(50, 560, 30):
        cv2.rectangle(page, (40, y), (440, y + 8), (20, 20, 20), -1)
    src = np.float32([[0, 0], [480, 0], [480, 600], [0, 600]])
    dst = np.float32([[120, 70], [470, 40], [520, 600], [60, 640]])
    shot = cv2.warpPerspective(
        page, cv2.getPerspectiveTransform(src, dst), (560, 680),
        borderMode=cv2.BORDER_CONSTANT, borderValue=(60, 66, 74),
    )
    ok, png = cv2.imencode(".png", shot)
    assert ok
    return png.tobytes()


def _upload(client, data: bytes, name="page.png", mime="image/png") -> dict:
    r = client.post("/api/v1/blobs", files={"file": (name, data, mime)})
    assert r.status_code == 200
    return r.json()


def test_the_registry_row_is_seeded_free_and_described(client):
    rows = client.get("/api/v1/registry/models").json()
    row = next(m for m in rows["items"] if m["model_id"] == "local/uvdoc")
    assert row["service_provider"] == "local"
    assert row["display_name"] == "UVDoc"
    assert row["tasks"] == ["image-to-image"]
    assert row["image_price"] == 0.0
    assert row["price_is_unknown"] is False
    assert row["price_is_verified"] is True
    assert row["tier"] == "free"
    assert row["input_map"] == {"image": "image"}
    assert row["extra_params"] == []
    assert "один лист" in row["description"]
    assert "50 мегапикселей" in row["description"]


def test_the_provider_appears_configured_without_a_key(client):
    caps = client.get("/api/v1/capabilities").json()
    local = next(p for p in caps["providers"] if p["id"] == "local")
    assert local["env_var"] is None
    assert local["tasks"] == ["image-to-image"]
    assert local["warnings"] == []
    assert caps["params_by_task"]["image-to-image"]["local"]["params"] == []


def test_a_page_goes_blob_run_poll_blob_and_leaves_a_zero_usage_row(client):
    _ready(client)
    blob = _upload(client, _photo_png())
    submitted = client.post(
        "/api/v1/runs",
        json={
            "task": "image-to-image",
            "model_id": "local/uvdoc",
            "provider": "local",
            "external_user_id": "local-accept",
            # Text the task only offers: reported as ignored, never refused.
            "text": "выпрями страницу",
            "attachments": [{"ref": blob["ref"], "kind": "image"}],
        },
    )
    assert submitted.status_code == 202
    state = _wait(client, submitted.json()["run_id"])
    assert state["status"] == "ok", state.get("error")
    result = state["result"]

    assert result["model"] == {"provider": "local", "model_id": "local/uvdoc",
                               "display_name": result["model"]["display_name"]}
    assert result["cost"]["value"] == 0.0
    assert result["cost"]["source"] == "computed"
    assert result["usage"]["images"] == 1
    assert result["ignored_params"] == ["prompt"]
    # The answer is a blob like any provider's: ref plus a link, PNG in → PNG out.
    out = result["attachments"][0]
    assert out["mime"] == "image/png"
    assert out["ref"] and out["download_url"] == f"/blobs/{out['ref']}"
    served = client.get(out["download_url"])
    assert served.status_code == 200
    assert served.content[:8] == b"\x89PNG\r\n\x1a\n"
    # The timeline shows the compute step of this provider.
    titles = [s["title"] for s in state["steps"]]
    assert "считаем на этом хосте" in titles

    row = client.get(f"/api/v1/logs/{state['log_id']}").json()
    assert row["status"] == "ok"
    assert row["model"]["provider"] == "local"
    assert row["cost"]["value"] == 0.0
    assert row["cost"]["source"] == "computed"

    usage = client.get("/api/v1/usage", params={"user": "local-accept"}).json()
    mine = [r for r in usage["items"] if r["bucket"] == "local-accept"]
    assert len(mine) == 1
    assert mine[0]["requests"] >= 1
    assert mine[0]["total_usd"] == 0.0


def test_a_jpeg_comes_back_as_jpeg(client):
    _ready(client)
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    png = _photo_png()
    ok, jpeg = cv2.imencode(".jpg", cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR))
    blob = _upload(client, jpeg.tobytes(), "page.jpg", "image/jpeg")
    state = _wait(client, client.post(
        "/api/v1/runs",
        json={"task": "image-to-image", "model_id": "local/uvdoc",
              "external_user_id": "local-jpeg",
              "attachments": [{"ref": blob["ref"], "kind": "image"}]},
    ).json()["run_id"])
    assert state["status"] == "ok", state.get("error")
    out = state["result"]["attachments"][0]
    assert out["mime"] == "image/jpeg"
    assert client.get(out["download_url"]).content[:3] == b"\xff\xd8\xff"


def test_missing_weights_answer_model_not_loaded_not_500(client, tmp_path, monkeypatch):
    if not get_core().adapters["local"].is_configured():
        pytest.skip("local runtime libraries not installed")
    adapter = get_core().adapters["local"]
    monkeypatch.setattr(adapter, "_dir", tmp_path)  # nothing installed here
    monkeypatch.setattr(adapter, "_handles", {})
    blob = _upload(client, _photo_png())
    state = _wait(client, client.post(
        "/api/v1/runs",
        json={"task": "image-to-image", "model_id": "local/uvdoc",
              "external_user_id": "local-noweights",
              "attachments": [{"ref": blob["ref"], "kind": "image"}]},
    ).json()["run_id"])
    assert state["status"] == "error"
    error = state["error"]["error"]
    assert error["code"] == "model_not_loaded"
    assert error["retriable"] is False
    assert error["log_id"] == state["log_id"]
    row = client.get(f"/api/v1/logs/{state['log_id']}").json()
    assert row["status"] == "error"


def test_a_frame_above_the_pixel_cap_is_refused_with_its_own_code(client):
    if not get_core().adapters["local"].is_configured():
        pytest.skip("local runtime libraries not installed")
    _ready(client)
    import struct

    header = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
              + struct.pack(">II", 9000, 7000) + b"\x08\x02\x00\x00\x00")
    blob = _upload(client, header, "huge.png")
    state = _wait(client, client.post(
        "/api/v1/runs",
        json={"task": "image-to-image", "model_id": "local/uvdoc",
              "external_user_id": "local-huge",
              "attachments": [{"ref": blob["ref"], "kind": "image"}]},
    ).json()["run_id"])
    assert state["status"] == "error"
    error = state["error"]["error"]
    assert error["code"] == "image_too_large"
    assert error["retriable"] is False


def test_three_runs_at_once_all_finish_while_the_service_keeps_answering(client):
    _ready(client)
    blob = _upload(client, _photo_png())
    run_ids = [
        client.post(
            "/api/v1/runs",
            json={"task": "image-to-image", "model_id": "local/uvdoc",
                  "external_user_id": "local-three", "batch_id": "local-three",
                  "attachments": [{"ref": blob["ref"], "kind": "image"}]},
        ).json()["run_id"]
        for _ in range(3)
    ]
    # While two compute and one waits for its slot, the process answers.
    t = time.monotonic()
    assert client.get("/health").status_code == 200
    assert time.monotonic() - t < 2.0
    states = [_wait(client, run_id) for run_id in run_ids]
    assert [s["status"] for s in states] == ["ok", "ok", "ok"]
    assert client.get("/api/v1/logs", params={"batch_id": "local-three"}).json()["total"] == 3
