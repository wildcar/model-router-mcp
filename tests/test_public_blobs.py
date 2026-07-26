"""The one route outside the auth gate, so it is checked from both directions.

It exists because Replicate takes file inputs as a URL and caps the inline form
at 256 KB. That is a good reason to open a door and no reason at all to leave it
open: a blob holds what a user asked and what a model answered.
"""

from __future__ import annotations

import time

import pytest

from router_mcp.config import Settings
from router_mcp.web import public_blobs

KEY = "test-signing-key"


def _settings(**kw) -> Settings:
    return Settings(
        PUBLIC_BLOB_BASE_URL="https://blob.example.org",
        PUBLIC_BLOB_SIGNING_KEY=KEY,
        PUBLIC_BLOB_URL_TTL_SECONDS=900,
        **kw,
    )


def _parts(url: str) -> tuple[str, str]:
    query = url.split("?", 1)[1]
    fields = dict(part.split("=", 1) for part in query.split("&"))
    return fields["exp"], fields["sig"]


def test_a_link_is_issued_and_verifies():
    settings = _settings()
    url = public_blobs.make_url(settings, "abc.png")
    assert url.startswith("https://blob.example.org/public/blobs/abc.png?")
    exp, sig = _parts(url)
    assert public_blobs.verify(settings, "abc.png", exp, sig) is None


def test_no_signing_key_means_no_link_and_no_access():
    """Fails closed: a missing setting must not turn into an open blob store."""
    settings = Settings(PUBLIC_BLOB_BASE_URL="https://blob.example.org")
    assert public_blobs.make_url(settings, "abc.png") is None
    assert public_blobs.verify(settings, "abc.png", "1", "x") is not None


def test_no_public_host_means_no_link():
    settings = Settings(PUBLIC_BLOB_SIGNING_KEY=KEY)
    assert public_blobs.make_url(settings, "abc.png") is None


def test_the_signature_covers_the_ref():
    """Otherwise one valid link would be a key to every file in the store."""
    settings = _settings()
    exp, sig = _parts(public_blobs.make_url(settings, "mine.png"))
    assert public_blobs.verify(settings, "someone-elses.png", exp, sig) == "подпись не сходится"


def test_the_signature_covers_the_deadline():
    settings = _settings()
    exp, sig = _parts(public_blobs.make_url(settings, "abc.png"))
    far_future = str(int(exp) + 10_000_000)
    assert public_blobs.verify(settings, "abc.png", far_future, sig) == "подпись не сходится"


def test_a_link_expires():
    settings = _settings()
    url = public_blobs.make_url(settings, "abc.png", now=time.time() - 100_000)
    exp, sig = _parts(url)
    assert public_blobs.verify(settings, "abc.png", exp, sig) == "срок ссылки истёк"


def test_an_unsigned_request_is_refused():
    settings = _settings()
    assert public_blobs.verify(settings, "abc.png", None, None) == "ссылка без подписи"
    assert public_blobs.verify(settings, "abc.png", "not-a-number", "x") == "ссылка испорчена"


def test_the_route_refuses_an_unsigned_link(client):
    """The suite configures no public host, so the door is shut entirely."""
    response = client.get("/public/blobs/whatever.png")
    assert response.status_code == 404
