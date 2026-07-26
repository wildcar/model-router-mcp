"""Serving response files safely.

A caller controls what a provider returns, and response payloads are served from
the same origin as the console — behind the Basic-auth session the browser
replays automatically. Anything that is not an image or audio therefore
downloads instead of rendering, and nothing is ever sniffed (§16.3).
"""

from __future__ import annotations

from router_mcp.runtime import get_core


def _save(client, data: bytes, mime: str) -> str:
    return get_core().blobs.save(data, mime)


def test_image_renders_inline_and_caches(client):
    ref = _save(client, b"\x89PNG\r\n", "image/png")
    r = client.get(f"/blobs/{ref}")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["x-content-type-options"] == "nosniff"
    # The ref is generated per payload, so the bytes behind it never change.
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["accept-ranges"] == "bytes"


def test_html_payload_downloads_instead_of_running(client):
    ref = _save(client, b"<script>alert(1)</script>", "text/html")
    r = client.get(f"/blobs/{ref}")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_range_request_is_partial(client):
    """The audio player has to seek without refetching the whole file."""
    ref = _save(client, b"0123456789", "audio/mpeg")
    r = client.get(f"/blobs/{ref}", headers={"Range": "bytes=2-5"})
    assert r.status_code == 206
    assert r.content == b"2345"
    assert r.headers["content-range"] == "bytes 2-5/10"


def test_unsatisfiable_range_falls_back_to_the_whole_file(client):
    ref = _save(client, b"0123456789", "audio/mpeg")
    r = client.get(f"/blobs/{ref}", headers={"Range": "bytes=99-"})
    assert r.status_code == 200
    assert r.content == b"0123456789"


def test_missing_blob_is_a_typed_404(client):
    r = client.get("/blobs/nope.bin")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
