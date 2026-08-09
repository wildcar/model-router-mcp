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


def test_a_signed_link_is_never_cached(client, monkeypatch):
    """The ordinary blob route marks payloads immutable and public, which is safe
    behind a password and wrong for a link that expires in minutes."""
    from router_mcp.runtime import get_core

    core = get_core()
    monkeypatch.setattr(core.settings, "public_blob_base_url", "https://blob.example.org")
    monkeypatch.setattr(core.settings, "public_blob_signing_key", KEY)
    ref = core.blobs.save(b"payload", "text/plain")
    url = public_blobs.make_url(core.settings, ref)
    path = url.split("https://blob.example.org", 1)[1]

    response = client.get(path)
    assert response.status_code == 200
    assert response.content == b"payload"
    assert response.headers["cache-control"] == "private, no-store"


# --- saying so before the money is spent -----------------------------------
# The failure these describe is entirely silent: the adapter's message is clear
# but only appears on the first real request from a phone, long after the key was
# entered, the model adopted and the operator convinced the installation works.
class _TakesFilesByLink:
    """A provider shaped like Replicate: files inline up to a cap, URL above it."""

    inline_file_limit_bytes = 256 * 1024

    def __init__(self, configured: bool = True) -> None:
        self._configured = configured

    def is_configured(self) -> bool:
        return self._configured


class _TakesNoFiles:
    inline_file_limit_bytes = None

    def is_configured(self) -> bool:
        return True


def _bare(**kw) -> Settings:
    kw.setdefault("PUBLIC_BLOB_BASE_URL", None)
    kw.setdefault("PUBLIC_BLOB_SIGNING_KEY", None)
    return Settings(**kw)


def test_both_settings_are_named_because_either_alone_delivers_nothing():
    assert public_blobs.missing_settings(_bare()) == [
        "PUBLIC_BLOB_BASE_URL", "PUBLIC_BLOB_SIGNING_KEY"
    ]
    half = _bare(PUBLIC_BLOB_BASE_URL="https://blob.example.org")
    assert public_blobs.missing_settings(half) == ["PUBLIC_BLOB_SIGNING_KEY"]
    # And a half-configured installation still issues no link at all.
    assert public_blobs.make_url(half, "abc.png") is None
    assert public_blobs.missing_settings(_settings()) == []


def test_a_provider_that_takes_files_by_link_is_warned_about():
    warning = public_blobs.provider_warning(_bare(), _TakesFilesByLink())
    assert "256 КБ" in warning
    assert "PUBLIC_BLOB_BASE_URL" in warning and "PUBLIC_BLOB_SIGNING_KEY" in warning


def test_nothing_is_said_when_there_is_nothing_to_say():
    """Three ways this is not a problem, and a warning in any of them is noise."""
    # Configured properly.
    assert public_blobs.provider_warning(_settings(), _TakesFilesByLink()) is None
    # The provider has no key, so it cannot be called at all — the console
    # already says «ключа нет» and a second line about files adds nothing.
    assert public_blobs.provider_warning(_bare(), _TakesFilesByLink(False)) is None
    # The provider does not take files by link.
    assert public_blobs.provider_warning(_bare(), _TakesNoFiles()) is None


def test_the_installation_roll_up_names_the_provider():
    warnings = public_blobs.installation_warnings(
        _bare(), {"replicate": _TakesFilesByLink(), "deepseek": _TakesNoFiles()}
    )
    assert len(warnings) == 1
    assert warnings[0].startswith("replicate: ")


def _pretend_replicate_is_configured(core, monkeypatch) -> None:
    """The real adapter with a token, and no public host. Patching the token
    rather than substituting a stub: the whole point is that the warning fires
    for the provider the installation actually carries."""
    monkeypatch.setattr(core.adapters["replicate"], "_api_token", "r8_test")
    monkeypatch.setattr(core.settings, "public_blob_base_url", None)
    monkeypatch.setattr(core.settings, "public_blob_signing_key", None)


def test_the_plan_always_carries_the_warnings_field(client):
    """The console reads it unconditionally; an absent key would be a branch on
    the server's age."""
    plan = client.post("/api/v1/plan", json={"task": "image-to-image"}).json()
    assert plan["warnings"] == []


def test_the_work_list_says_it_when_files_cannot_be_delivered(client, monkeypatch):
    from router_mcp.runtime import get_core

    core = get_core()
    _pretend_replicate_is_configured(core, monkeypatch)
    work = client.get("/api/v1/health/summary").json()["work"]
    assert any("PUBLIC_BLOB_BASE_URL" in item["text"] for item in work)


def test_a_provider_carries_its_own_warning_in_capabilities(client, monkeypatch):
    """Configured and still unable to do what it was added for: the console needs
    its own word for that, and «настроен» is not it."""
    from router_mcp.runtime import get_core

    core = get_core()
    _pretend_replicate_is_configured(core, monkeypatch)
    caps = client.get("/api/v1/capabilities").json()
    replicate = next(p for p in caps["providers"] if p["id"] == "replicate")
    assert replicate["warnings"] and "PUBLIC_BLOB_BASE_URL" in replicate["warnings"][0]


def test_the_plan_flags_it_for_a_task_that_carries_a_file(client, monkeypatch):
    """Через `_plan_warnings` напрямую: план предупреждает про того поставщика,
    который действительно был бы вызван, и только для задачи с файлом."""
    from types import SimpleNamespace

    from router_mcp.domain.task import Task
    from router_mcp.runtime import get_core
    from router_mcp.web.api import _plan_warnings

    core = get_core()
    _pretend_replicate_is_configured(core, monkeypatch)
    fake = SimpleNamespace(
        adapters=core.adapters, settings=core.settings
    )
    plan = {"will_call": {"provider": "replicate", "model_id": "x/y"}}
    assert "PUBLIC_BLOB_BASE_URL" in _plan_warnings(fake, Task.IMAGE_TO_IMAGE, plan)[0]
    # Text in, text out: none of this applies and a warning would be noise.
    assert _plan_warnings(fake, Task.TEXT_TO_TEXT, plan) == []
    # Nothing would be called: the plan already says so, in its own field.
    assert _plan_warnings(fake, Task.IMAGE_TO_IMAGE, {"will_call": None}) == []
