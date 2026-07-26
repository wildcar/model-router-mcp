"""The HTTP surface of wave 3: /runs, /blobs, /retention.

No provider is configured in the test app, so every run fails fast — which is
the useful case here: what matters is that the run is accepted, polled, finished
and recorded, not what the provider would have answered.
"""

from __future__ import annotations

import time


def _wait(client, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/runs/{run_id}").json()
        if state["status"] in ("ok", "error", "cancelled"):
            return state
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish in {timeout}s")


def test_a_run_is_accepted_before_it_is_executed(client):
    r = client.post(
        "/api/v1/runs",
        json={"modality": "chat", "text": "привет", "external_user_id": "runs-api"},
        headers={"X-Router-Channel": "ui"},
    )
    assert r.status_code == 202
    submitted = r.json()
    assert submitted["run_id"]
    assert submitted["status"] in ("queued", "running")

    state = _wait(client, submitted["run_id"])
    # No key is configured in tests, so the run fails — with the error envelope
    # the console renders, and a log id to open it by.
    assert state["status"] == "error"
    assert state["error"]["error"]["code"]
    assert state["log_id"]


def test_the_run_leaves_one_row_in_the_log(client):
    run = client.post(
        "/api/v1/runs",
        json={"modality": "chat", "text": "раз", "external_user_id": "runs-one-row"},
    ).json()
    state = _wait(client, run["run_id"])
    rows = client.get("/api/v1/logs", params={"user": "runs-one-row"}).json()
    assert rows["total"] == 1
    assert rows["items"][0]["id"] == state["log_id"]
    assert rows["items"][0]["status"] == "error"
    assert rows["items"][0]["channel"] == "api"


def test_runs_of_one_batch_are_listed_together(client):
    for text in ("а", "б"):
        client.post(
            "/api/v1/runs",
            json={
                "modality": "chat",
                "text": text,
                "external_user_id": "runs-batch",
                "batch_id": "batch-7",
            },
        )
    listed = client.get("/api/v1/runs", params={"batch_id": "batch-7"}).json()
    assert listed["total"] == 2
    assert {r["batch_id"] for r in listed["items"]} == {"batch-7"}

    # And the batch is a filter on the log too, so a reload finds it again.
    for run in listed["items"]:
        _wait(client, run["run_id"])
    logged = client.get("/api/v1/logs", params={"batch_id": "batch-7"}).json()
    assert logged["total"] == 2


def test_the_same_key_twice_returns_the_same_run(client):
    body = {
        "modality": "chat",
        "text": "идемпотентно",
        "external_user_id": "runs-idem",
        "client_request_id": "key-42",
    }
    submitted = client.post("/api/v1/runs", json=body).json()
    first = _wait(client, submitted["run_id"])
    again = client.post("/api/v1/runs", json=body).json()
    assert again["log_id"] == first["log_id"]
    assert client.get("/api/v1/logs", params={"user": "runs-idem"}).json()["total"] == 1


def test_cancelling_a_finished_run_is_a_conflict(client):
    run = client.post(
        "/api/v1/runs",
        json={"modality": "chat", "text": "поздно", "external_user_id": "runs-late"},
    ).json()
    _wait(client, run["run_id"])
    r = client.delete(f"/api/v1/runs/{run['run_id']}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_an_unknown_run_is_a_404(client):
    assert client.get("/api/v1/runs/nope").status_code == 404


def test_transcription_needs_a_file(client):
    r = client.post(
        "/api/v1/runs", json={"modality": "stt", "external_user_id": "runs-stt"}
    )
    assert r.status_code == 400
    assert "blobs" in r.json()["error"]["message"]


def test_a_file_is_uploaded_once_and_referenced_by_id(client):
    """Attachments by reference: the asynchronous run path carries JSON only."""
    r = client.post(
        "/api/v1/blobs", files={"file": ("hi.ogg", b"not really audio", "audio/ogg")}
    )
    assert r.status_code == 200
    blob = r.json()
    assert blob["bytes"] == 16
    assert blob["mime"] == "audio/ogg"
    # The stored file is served back by ref.
    assert client.get(blob["url"]).content == b"not really audio"

    run = client.post(
        "/api/v1/runs",
        json={
            "modality": "stt",
            "external_user_id": "runs-stt-ref",
            "attachment_ref": blob["ref"],
        },
    ).json()
    state = _wait(client, run["run_id"])
    # Nothing is configured to transcribe it, but the audio reached the router:
    # the run has a row of its own and the failure is the provider's, not the
    # request's.
    assert state["status"] == "error"
    row = client.get(f"/api/v1/logs/{state['log_id']}").json()
    assert row["modality"] == "stt"


def test_a_missing_ref_is_a_404(client):
    r = client.post(
        "/api/v1/runs",
        json={"modality": "stt", "attachment_ref": "deadbeef.ogg"},
    )
    assert r.status_code == 404


def test_retention_reports_before_it_deletes(client):
    state = client.get("/api/v1/retention").json()
    # Nothing is configured, so the policy is "keep everything" and a purge
    # would remove no rows at all.
    assert state["log_retention_days"] == 0
    assert state["logs_to_delete"] == 0
    assert state["total_logs"] >= 1

    purged = client.post("/api/v1/retention/purge", params={"orphans": False}).json()
    assert purged["deleted_logs"] == 0
    assert purged["deleted_blobs"] == 0
    assert client.get("/api/v1/logs").json()["total"] == state["total_logs"]
