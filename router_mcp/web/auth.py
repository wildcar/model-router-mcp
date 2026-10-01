"""Console login: a form on /login and a signed session cookie.

This replaces nginx's HTTP Basic auth. The browser's Basic dialog is not a web
page, so password managers fill it only sometimes; a plain form with
`autocomplete="username"` / `"current-password"` they fill every time.

Users and passwords stay where they were: the htpasswd file nginx used
(`WEB_AUTH_HTPASSWD`), so `htpasswd` remains the way to add a user or change a
password. The file is re-read when its mtime changes.

The cookie carries `user.expiry.signature`, HMAC-SHA256 over the user, the
expiry and that user's current htpasswd hash: changing a password ends every
session signed for the old one, with no session table to keep.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from router_mcp.config import Settings
from router_mcp.web.pages import STATIC_VERSION, templates

logger = logging.getLogger(__name__)

COOKIE_NAME = "mr_session"
LOGIN_PATH = "/login"

# Paths the gate never asks about. /mcp has its own Bearer token, /health is a
# public liveness probe, /public/blobs is HMAC-signed per link, and the login
# page needs its two stylesheets before anyone is signed in.
OPEN_PATHS = ("/health", LOGIN_PATH, "/static/app.css")
OPEN_PREFIXES = ("/mcp/", "/public/blobs/", "/static/css/")

# Failed sign-ins per client address before /login stops checking passwords.
MAX_FAILURES = 10
FAILURE_WINDOW_S = 600

LOOPBACK = {"127.0.0.1", "::1"}


# --- password hashes ------------------------------------------------------

_ITOA64 = b"./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _to64(value: int, length: int) -> bytes:
    out = bytearray()
    for _ in range(length):
        out.append(_ITOA64[value & 0x3F])
        value >>= 6
    return bytes(out)


def md5_crypt(password: bytes, salt: bytes, magic: bytes = b"$apr1$") -> str:
    """MD5-crypt as Apache's `htpasswd -m` writes it (`$apr1$`, or `$1$`)."""
    salt = salt[:8]
    ctx = hashlib.md5(password + magic + salt)
    alt = hashlib.md5(password + salt + password).digest()
    for i in range(len(password), 0, -16):
        ctx.update(alt[: min(16, i)])
    i = len(password)
    while i:
        ctx.update(b"\0" if i & 1 else password[:1])
        i >>= 1
    final = ctx.digest()
    for i in range(1000):
        step = hashlib.md5()
        step.update(password if i & 1 else final)
        if i % 3:
            step.update(salt)
        if i % 7:
            step.update(password)
        step.update(final if i & 1 else password)
        final = step.digest()
    encoded = b"".join(
        _to64((final[a] << 16) | (final[b] << 8) | final[c], 4)
        for a, b, c in ((0, 6, 12), (1, 7, 13), (2, 8, 14), (3, 9, 15), (4, 10, 5))
    ) + _to64(final[11], 2)
    return (magic + salt + b"$" + encoded).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    pw = password.encode("utf-8")
    if hashed.startswith(("$apr1$", "$1$")):
        magic = b"$apr1$" if hashed.startswith("$apr1$") else b"$1$"
        salt = hashed[len(magic):].split("$", 1)[0].encode("ascii")
        return hmac.compare_digest(md5_crypt(pw, salt, magic), hashed)
    if hashed.startswith("{SHA}"):
        digest = base64.b64encode(hashlib.sha1(pw).digest()).decode("ascii")
        return hmac.compare_digest("{SHA}" + digest, hashed)
    if hashed.startswith(("$2y$", "$2b$", "$2a$")):
        try:
            import bcrypt  # optional: `htpasswd -B` lines need it
        except ImportError:
            logger.warning("htpasswd has a bcrypt line but the bcrypt module is not installed")
            return False
        return bcrypt.checkpw(pw, hashed.replace("$2y$", "$2b$", 1).encode("ascii"))
    logger.warning("htpasswd line in an unsupported format (crypt/plain); refused")
    return False


class Htpasswd:
    """The user file, re-read whenever it changes on disk."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._mtime: float | None = None
        self._entries: dict[str, str] = {}

    def entries(self) -> dict[str, str]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            logger.warning("htpasswd %s is unreadable; nobody can sign in", self.path)
            return {}
        if mtime != self._mtime:
            entries: dict[str, str] = {}
            for line in self.path.read_text(encoding="utf-8").splitlines():
                user, sep, hashed = line.strip().partition(":")
                if sep and user and not user.startswith("#"):
                    entries[user] = hashed
            self._entries, self._mtime = entries, mtime
        return self._entries

    def get(self, user: str) -> str | None:
        return self.entries().get(user)


# --- the gate -------------------------------------------------------------


def _load_secret(settings: Settings) -> bytes:
    if settings.web_auth_secret:
        return settings.web_auth_secret.encode("utf-8")
    key_path = Path(settings.db_path).resolve().parent / "web_session.key"
    try:
        return bytes.fromhex(key_path.read_text().strip())
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex())
    return key


class WebAuth:
    """Everything the gate and the login page share; `None` when it is off."""

    def __init__(self, settings: Settings) -> None:
        self.users = Htpasswd(settings.web_auth_htpasswd or "")
        self.secret = _load_secret(settings)
        self.session_s = max(1, settings.web_auth_session_days) * 86400
        self.trust_loopback = settings.web_auth_trust_loopback
        self._failures: dict[str, list[float]] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "WebAuth | None":
        return cls(settings) if settings.web_auth_htpasswd else None

    # sessions

    def _sign(self, user: str, expires: int, hashed: str) -> str:
        msg = f"{user}\n{expires}\n{hashed}".encode("utf-8")
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def issue(self, user: str, now: float | None = None) -> str:
        expires = int(now if now is not None else time.time()) + self.session_s
        hashed = self.users.get(user) or ""
        name = base64.urlsafe_b64encode(user.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{name}.{expires}.{self._sign(user, expires, hashed)}"

    def session_user(self, cookie: str | None, now: float | None = None) -> str | None:
        if not cookie:
            return None
        try:
            name, expires_s, sig = cookie.split(".")
            user = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode("utf-8")
            expires = int(expires_s)
        except ValueError:
            return None
        if expires < (now if now is not None else time.time()):
            return None
        hashed = self.users.get(user)
        if hashed is None:
            return None
        if not hmac.compare_digest(sig, self._sign(user, expires, hashed)):
            return None
        return user

    # who may skip the gate

    def is_open_path(self, path: str) -> bool:
        return path in OPEN_PATHS or path == "/mcp" or path.startswith(OPEN_PREFIXES)

    def is_local_caller(self, scope) -> bool:
        if not self.trust_loopback:
            return False
        client = scope.get("client")
        if not client or client[0] not in LOOPBACK:
            return False
        names = {k for k, _ in scope.get("headers") or []}
        return b"x-real-ip" not in names and b"x-forwarded-for" not in names

    # brute-force brake

    def blocked(self, ip: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        recent = [t for t in self._failures.get(ip, []) if now - t < FAILURE_WINDOW_S]
        self._failures[ip] = recent
        return len(recent) >= MAX_FAILURES

    def record_failure(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.time())

    def check(self, user: str, password: str) -> bool:
        hashed = self.users.get(user)
        if hashed is None:
            # Same work for an unknown user, so timing does not tell which it was.
            verify_password(password, "$apr1$xxxxxxxx$" + "x" * 22)
            return False
        return verify_password(password, hashed)


def login_redirect(path: str, query: str) -> str:
    target = path + (f"?{query}" if query else "")
    return f"{LOGIN_PATH}?next={quote(target, safe='')}" if target != "/" else LOGIN_PATH


def _safe_next(value: str | None) -> str:
    # Only a path on this site: `//evil.example` and `https://…` would make the
    # login form an open redirect.
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


# --- pages ----------------------------------------------------------------

auth_router = APIRouter()


def _login_page(request: Request, *, next_path: str, user: str = "", error: str = "",
                status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_path, "user": user, "error": error, "version": STATIC_VERSION},
        status_code=status_code,
    )


@auth_router.get(LOGIN_PATH, response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    auth: WebAuth | None = request.app.state.web_auth
    next_path = _safe_next(request.query_params.get("next"))
    if auth is None or auth.session_user(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(next_path, status_code=303)
    return _login_page(request, next_path=next_path)


@auth_router.post(LOGIN_PATH, response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    auth: WebAuth | None = request.app.state.web_auth
    form = await request.form()
    next_path = _safe_next(str(form.get("next") or ""))
    if auth is None:
        return RedirectResponse(next_path, status_code=303)
    user = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    ip = _client_ip(request)
    if auth.blocked(ip):
        logger.warning("Console login throttled for %s", ip)
        return _login_page(
            request, next_path=next_path, user=user, status_code=429,
            error="Слишком много неудачных попыток. Подождите десять минут.",
        )
    if not auth.check(user, password):
        auth.record_failure(ip)
        logger.info("Console login failed for user %r from %s", user, ip)
        return _login_page(
            request, next_path=next_path, user=user, status_code=401,
            error="Неверное имя или пароль.",
        )
    logger.info("Console login: %r from %s", user, ip)
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        COOKIE_NAME, auth.issue(user), max_age=auth.session_s, path="/",
        httponly=True, secure=_is_https(request), samesite="lax",
    )
    return response


@auth_router.post("/logout")
async def logout(request: Request) -> Response:
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
