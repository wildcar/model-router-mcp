"""The task field on the HTTP surface, and the compatibility that goes with it.

Callers written before ADR-0006 send `modality`. They are a published contract,
so every one of these has to keep working unchanged.
"""

from __future__ import annotations

import pytest


def test_plan_accepts_a_task(client):
    body = {"task": "text-to-image", "text": "a cat"}
    plan = client.post("/api/v1/plan", json=body).json()
    assert plan["task"] == "text-to-image"
    assert plan["modality"] == "image"


def test_plan_still_accepts_a_modality(client):
    """`modality: "chat"` has to mean `text-to-text` forever."""
    plan = client.post("/api/v1/plan", json={"modality": "chat", "text": "hi"}).json()
    assert plan["task"] == "text-to-text"


def test_an_explicit_task_wins_over_a_modality(client):
    body = {"task": "image-to-image", "modality": "chat", "text": "hi"}
    plan = client.post("/api/v1/plan", json=body).json()
    assert plan["task"] == "image-to-image"


def test_an_unknown_task_is_a_client_error(client):
    r = client.post("/api/v1/plan", json={"task": "text-to-hologram", "text": "hi"})
    assert r.status_code == 400
    assert "text-to-hologram" in r.json()["error"]["message"]


def test_a_task_that_needs_a_file_says_which_medium_is_missing(client):
    """The old check was hard-coded for распознавание, so editing an image had no
    message at all."""
    r = client.post("/api/v1/runs", json={"task": "image-to-image", "text": "make it red"})
    assert r.status_code == 400
    message = r.json()["error"]["message"]
    assert "image" in message
    assert "/api/v1/blobs" in message


def test_transcription_still_reports_its_missing_file(client):
    r = client.post("/api/v1/runs", json={"task": "speech-to-text"})
    assert r.status_code == 400
    assert "audio" in r.json()["error"]["message"]


def test_capabilities_describes_every_task(client):
    caps = client.get("/api/v1/capabilities").json()
    assert caps["schema_version"] == 2
    image_to_image = caps["tasks"]["image-to-image"]
    assert image_to_image["requires"] == ["image", "text"]
    assert image_to_image["produces"] == ["image"]
    assert image_to_image["billing_group"] == "image"


def test_capabilities_still_carries_the_group_keyed_matrix(client):
    """A page loaded before the restart reads `params`; it must not vanish."""
    caps = client.get("/api/v1/capabilities").json()
    assert set(caps["params"]) <= {"chat", "image", "stt", "tts", "cli", "audio", "video"}
    assert caps["params_by_task"]


def test_a_provider_lists_its_tasks(client):
    providers = {p["id"]: p for p in client.get("/api/v1/capabilities").json()["providers"]}
    codex = providers["codex-oauth"]
    assert codex["tasks"] == ["text-to-image"]
    assert codex["modalities"] == ["image"]


def test_the_log_can_be_filtered_by_task(client):
    r = client.get("/api/v1/logs?task=text-to-text")
    assert r.status_code == 200
    assert "items" in r.json()


def test_registry_facets_count_tasks(client):
    facets = client.get("/api/v1/registry/models").json()["facets"]
    assert "task" in facets
    assert "modality" in facets


def test_a_manual_model_can_be_added_by_task(client):
    body = {
        "service_provider_id": "openrouter",
        "model_id": "vendor/editor",
        "tasks": ["image-to-image"],
        "image_price": 0.04,
    }
    created = client.post("/api/v1/registry/models", json=body)
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["tasks"] == ["image-to-image"]
    # The billing group follows from the task rather than being asked for twice.
    assert row["modalities"] == ["image"]
