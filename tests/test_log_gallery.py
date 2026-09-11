"""The pictures side of the log: the gallery feed, the entry's neighbours, and
the thumbnails both are drawn from.

Seeded with plain sqlite3 against the shared app database, under a user label
of its own (`gallery-check`), so the counts other modules assert on their own
labels are untouched. The gallery endpoint has no filter, so its assertions
look for these rows rather than counting the answer.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from router_mcp.web import thumbs
from router_mcp.web.views import asked_words, image_card, run_from_log

USER = "gallery-check"


@pytest.fixture(scope="module", autouse=True)
def seeded(client, app_db_path):
    rows = [
        # A drawn picture: the response ref is the only trace of it.
        dict(
            id=90001,
            status="ok",
            task="text-to-image",
            request_text="рыжий кот на подоконнике",
            request_json=json.dumps({"text": "рыжий кот на подоконнике", "attachments": []}),
            response_ref="a1a1.png",
            request_ref=None,
        ),
        # An edit: two photos in, one picture out, and the row keeps the first
        # photo's ref in its own column — the second lives only in the snapshot.
        dict(
            id=90002,
            status="ok",
            task="image-to-image",
            request_text=None,
            request_json=json.dumps({
                "text": None,
                "messages": None,
                "attachments": [
                    {"kind": "image", "role": "person", "mime": "image/jpeg", "ref": "b1b1.jpg"},
                    {"kind": "image", "role": "garment", "mime": "image/jpeg", "ref": "b2b2.jpg"},
                ],
            }),
            response_ref="b3b3.webp",
            request_ref="b1b1.jpg",
        ),
        # A chat with history and no pictures: not a card.
        dict(
            id=90003,
            status="ok",
            task="text-to-text",
            request_text=json.dumps([
                {"role": "system", "content": "будь кратким"},
                {"role": "user", "content": "сколько будет 2+2"},
            ], ensure_ascii=False),
            request_json=None,
            response_ref=None,
            request_ref=None,
        ),
        # A failed drawing: the request had words, the answer had nothing.
        dict(
            id=90004,
            status="error",
            task="text-to-image",
            request_text="закат над морем",
            request_json=json.dumps({"text": "закат над морем", "attachments": []}),
            response_ref=None,
            request_ref=None,
        ),
        # Audio in, text out: a ref that is not a picture must not become one.
        dict(
            id=90005,
            status="ok",
            task="speech-to-text",
            request_text=None,
            request_json=json.dumps({"attachments": [
                {"kind": "audio", "mime": "audio/mpeg", "ref": "c1c1.mp3"}]}),
            response_ref=None,
            request_ref="c1c1.mp3",
        ),
    ]
    conn = sqlite3.connect(app_db_path)
    with conn:
        for i, row in enumerate(rows):
            conn.execute(
                """
                INSERT INTO request_logs (
                    id, external_user_id, modality, task, service_provider_id, model_id,
                    status, channel, request_text, request_json, request_ref,
                    response_ref, cost_usd, cost_source, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["id"], USER, "image", row["task"], "codex-oauth", "gpt-image-2",
                    row["status"], "api", row["request_text"], row["request_json"],
                    row["request_ref"], row["response_ref"], 0.0, "computed",
                    f"2026-09-11T08:0{i}:00+00:00",
                ),
            )
    conn.close()
    yield
    # Leave the shared database as it was found: other modules count their own
    # rows against the whole table, whichever order pytest runs the files in.
    conn = sqlite3.connect(app_db_path)
    with conn:
        conn.execute("DELETE FROM request_logs WHERE external_user_id = ?", (USER,))
    conn.close()


# --- the feed -----------------------------------------------------------------
def test_gallery_lists_pictures_newest_first_one_card_per_row(client):
    body = client.get("/api/v1/logs/images?limit=10").json()
    assert set(body) == {"items", "total", "next_cursor", "facets"}
    assert body["total"] is None  # counting would scan every snapshot
    ours = [item for item in body["items"] if item["external_user_id"] == USER]
    assert [item["log_id"] for item in ours] == [90002, 90001]
    edit, drawing = ours
    assert [(img["side"], img["role"], img["ref"]) for img in edit["images"]] == [
        ("request", "person", "b1b1.jpg"),
        ("request", "garment", "b2b2.jpg"),
        ("response", None, "b3b3.webp"),
    ]
    assert edit["images"][0]["thumb_url"] == "/blobs/b1b1.jpg/thumb"
    assert edit["images"][0]["url"] == "/blobs/b1b1.jpg"
    assert drawing["images"] == [{
        "side": "response", "role": None, "ref": "a1a1.png", "mime": "image/png",
        "url": "/blobs/a1a1.png", "thumb_url": "/blobs/a1a1.png/thumb",
    }]
    assert drawing["text"] == "рыжий кот на подоконнике"
    assert drawing["model_id"] == "gpt-image-2"


def test_gallery_pages_by_cursor(client):
    first = client.get("/api/v1/logs/images?limit=1").json()
    assert first["next_cursor"] == first["items"][0]["log_id"]
    second = client.get(f"/api/v1/logs/images?limit=1&cursor={first['next_cursor']}").json()
    assert second["items"][0]["log_id"] < first["items"][0]["log_id"]


def test_rows_without_a_picture_are_not_cards(client):
    ids = {item["log_id"] for item in client.get("/api/v1/logs/images?limit=100").json()["items"]}
    assert 90003 not in ids  # chat
    assert 90004 not in ids  # failed drawing, nothing came back
    assert 90005 not in ids  # audio


def test_asked_words_come_from_the_snapshot_or_the_history():
    assert asked_words({"request_json": json.dumps({"text": "закат"}), "request_text": "x"}) == "закат"
    history = json.dumps([{"role": "system", "content": "s"}, {"role": "user", "content": "2+2"}])
    assert asked_words({"request_json": None, "request_text": history}) == "2+2"
    assert asked_words({"request_json": None, "request_text": "plain  words\n here"}) == "plain words here"
    long = "a" * 500
    assert len(asked_words({"request_json": None, "request_text": long})) == 200


def test_card_skips_a_cut_data_url_and_keeps_a_real_one():
    row = {
        "id": 1, "request_json": json.dumps({"attachments": [
            {"kind": "image", "mime": "image/png", "url": "data:image/png;base64,AAAA"},
            {"kind": "image", "mime": "image/png", "url": "https://example.org/cat.png"},
        ]}),
    }
    images = image_card(row)["images"]
    assert [img["url"] for img in images] == ["https://example.org/cat.png"]
    assert images[0]["thumb_url"] == "https://example.org/cat.png"


# --- the entry knows what it holds ---------------------------------------------
def test_log_row_names_its_picture_by_the_ref_extension():
    run = run_from_log({"id": 5, "status": "ok", "response_ref": "a1a1.png",
                        "request_ref": "b1b1.jpg"})
    assert run["attachments"][0]["kind"] == "image"
    assert run["attachments"][0]["mime"] == "image/png"
    # An old row without a snapshot still says what its uploaded file was.
    assert run["request"]["attachments"][0]["kind"] == "image"
    assert run["request"]["attachments"][0]["mime"] == "image/jpeg"
    audio = run_from_log({"id": 6, "status": "ok", "response_ref": "c1c1.mp3"})
    assert audio["attachments"][0]["kind"] == "audio"
    other = run_from_log({"id": 7, "status": "ok", "response_ref": "d1d1.bin"})
    assert other["attachments"][0]["kind"] == "file"
    assert other["attachments"][0]["mime"] in (None, "application/octet-stream")


def test_the_entry_from_the_api_shows_its_picture(client):
    run = client.get("/api/v1/logs/90001").json()
    assert run["attachments"] == [{
        "kind": "image", "mime": "image/png", "url": None, "ref": "a1a1.png",
        "download_url": "/blobs/a1a1.png",
    }]


# --- neighbours ------------------------------------------------------------------
def test_neighbors_walk_the_filtered_list(client):
    both = client.get(f"/api/v1/logs/90003/neighbors?user={USER}").json()
    assert both == {"newer_id": 90004, "older_id": 90002}
    newest = client.get(f"/api/v1/logs/90005/neighbors?user={USER}").json()
    assert newest["newer_id"] is None
    assert newest["older_id"] == 90004
    # With a filter the next row is the next row *that matches*.
    errors = client.get(f"/api/v1/logs/90005/neighbors?user={USER}&status=error").json()
    assert errors == {"newer_id": None, "older_id": 90004}
    only_ok = client.get(f"/api/v1/logs/90004/neighbors?user={USER}&status=ok").json()
    assert only_ok == {"newer_id": 90005, "older_id": 90003}


def test_neighbors_of_an_unknown_id_are_still_an_answer(client):
    # Not a 404: the id may have been purged by retention while the window was
    # open, and the arrows should still lead somewhere.
    body = client.get(f"/api/v1/logs/99999999/neighbors?user={USER}").json()
    assert body["newer_id"] is None
    assert body["older_id"] == 90005


# --- thumbnails ---------------------------------------------------------------------
def _png(width: int, height: int) -> bytes:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:, :, 2] = 200
    ok, buffer = cv2.imencode(".png", canvas)
    assert ok
    return buffer.tobytes()


def test_thumbnail_is_bounded_and_small_pictures_are_their_own():
    small = thumbs.make(_png(200, 100))
    assert small is None
    body, mime = thumbs.make(_png(1024, 1536))
    assert mime == "image/jpeg"
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    decoded = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert decoded.shape[:2] == (320, 213)
    assert thumbs.make(b"not a picture") is None


def test_thumb_route_serves_a_preview_and_refuses_non_images(client):
    ref = client.post(
        "/api/v1/blobs", files={"file": ("cat.png", _png(1024, 768), "image/png")}
    ).json()["ref"]
    r = client.get(f"/blobs/{ref}/thumb")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert "immutable" in r.headers["cache-control"]
    assert len(r.content) < len(client.get(f"/blobs/{ref}").content)
    # Second read comes from the cache and is byte-identical.
    assert client.get(f"/blobs/{ref}/thumb").content == r.content

    text_ref = client.post(
        "/api/v1/blobs", files={"file": ("a.txt", b"hello", "text/plain")}
    ).json()["ref"]
    assert client.get(f"/blobs/{text_ref}/thumb").status_code == 404
    assert client.get("/blobs/nope.png/thumb").status_code == 404
