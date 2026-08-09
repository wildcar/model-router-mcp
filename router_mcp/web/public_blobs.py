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
from typing import Any

from router_mcp.config import Settings


def missing_settings(settings: Settings) -> list[str]:
    """Environment variables still needed before any public link can be issued.

    Both, not one: a base URL without a signing key issues nothing (fail closed,
    see `make_url`), so naming only the URL would send an operator to set half of
    it and watch the same failure again.
    """
    missing = []
    if not (settings.public_blob_base_url or "").strip():
        missing.append("PUBLIC_BLOB_BASE_URL")
    if not (settings.public_blob_signing_key or "").strip():
        missing.append("PUBLIC_BLOB_SIGNING_KEY")
    return missing


def provider_warning(settings: Settings, adapter: object) -> str | None:
    """What is wrong for this provider, in the operator's words, or None.

    The failure it prevents is entirely silent until money has been spent: the
    adapter's message is clear, but it only appears on the first real request
    from a phone, long after the key was entered and the model was adopted.
    """
    limit = getattr(adapter, "inline_file_limit_bytes", None)
    if limit is None or not adapter.is_configured():  # type: ignore[attr-defined]
        return None
    missing = missing_settings(settings)
    if not missing:
        return None
    return (
        f"Файлы больше {limit // 1024} КБ этот поставщик принимает только ссылкой, "
        f"а публичная выдача не настроена: не задано {', '.join(missing)}. "
        f"Снимок с телефона — это 2–5 МБ, то есть не уйдёт ни один."
    )


def installation_warnings(settings: Settings, adapters: dict[str, Any]) -> list[str]:
    """The same warning, once per affected provider, for the whole installation."""
    out = []
    for provider_id in sorted(adapters):
        warning = provider_warning(settings, adapters[provider_id])
        if warning:
            out.append(f"{provider_id}: {warning}")
    return out


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
