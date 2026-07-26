"""Signed, expiring links to a stored file, for providers that fetch by URL.

Replicate takes file inputs as a URL or as a data URI capped at 256 KB, and a
photograph from the console is bigger than that. So the file has to be reachable
from outside — which is the whole reason this module is careful.

Two rules it exists to enforce:

* a link names one ref and expires. Blobs hold what users asked and what models
  answered; "the blob store is public" and "this file, for the next quarter of an
  hour" are different things, and only the second is acceptable;
* no signing key, no links. An unsigned public path would serve any ref to
  anyone who guessed one, and a missing setting must fail closed.

The signature covers the ref and the deadline together, so neither can be edited
without invalidating it.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from router_mcp.config import Settings


def sign(ref: str, expires_at: int, key: str) -> str:
    message = f"{ref}:{expires_at}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()[:32]


def make_url(settings: Settings, ref: str, *, now: float | None = None) -> str | None:
    """A public link to one blob, or None when the installation has no public
    host or no signing key. None is not an error: it means "hand the bytes over
    inline instead", and the caller says so if they do not fit."""
    base = (settings.public_blob_base_url or "").rstrip("/")
    key = settings.public_blob_signing_key
    if not base or not key:
        return None
    expires_at = int((now if now is not None else time.time())) + max(
        60, settings.public_blob_url_ttl_seconds
    )
    return f"{base}/public/blobs/{ref}?exp={expires_at}&sig={sign(ref, expires_at, key)}"


def verify(settings: Settings, ref: str, exp: str | None, sig: str | None, *,
           now: float | None = None) -> str | None:
    """None when the link is good, otherwise the reason it is not.

    Compared in constant time, and the deadline is checked *after* the signature
    so a forged link cannot be told from an expired one by timing the answer.
    """
    key = settings.public_blob_signing_key
    if not key or not settings.public_blob_base_url:
        return "публичная выдача файлов не настроена"
    if not exp or not sig:
        return "ссылка без подписи"
    try:
        expires_at = int(exp)
    except ValueError:
        return "ссылка испорчена"
    if not hmac.compare_digest(sig, sign(ref, expires_at, key)):
        return "подпись не сходится"
    if expires_at < (now if now is not None else time.time()):
        return "срок ссылки истёк"
    return None
