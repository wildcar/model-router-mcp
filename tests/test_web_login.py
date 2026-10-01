"""The console login: /login form, session cookie, and the gate in front.

Built on a small FastAPI app carrying only the middleware and the login routes,
for the reason tests/test_auth.py gives: a second FastMCP app cannot start.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from router_mcp.app import WebAuthMiddleware
from router_mcp.config import Settings
from router_mcp.web.auth import COOKIE_NAME, WebAuth, auth_router, md5_crypt, verify_password

PASSWORD = "пароль test"
# `openssl passwd -apr1 -salt abcdefgh 'пароль test'`
APR1 = "$apr1$abcdefgh$47NGUA/61QV9ff6aXK/Lx/"


def _settings(tmp_path, **extra) -> Settings:
    htpasswd = tmp_path / "users"
    htpasswd.write_text(f"admin:{APR1}\n")
    return Settings(
        DB_PATH=str(tmp_path / "router.db"),
        WEB_AUTH_HTPASSWD=str(htpasswd),
        WEB_AUTH_SECRET=None,
        **extra,
    )


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.web_auth = WebAuth.from_settings(settings)
    app.add_middleware(WebAuthMiddleware, auth=app.state.web_auth)
    app.include_router(auth_router)

    async def ok():
        return PlainTextResponse("ok")

    for path in ("/overview", "/api/v1/health/summary", "/health", "/mcp/", "/static/app.css",
                 "/static/js/pages/run.js", "/public/blobs/abc"):
        app.add_api_route(path, ok, methods=["GET", "POST"])
    return app


@pytest.fixture
def client(tmp_path):
    return TestClient(_app(_settings(tmp_path)), follow_redirects=False)


def _login(client, password=PASSWORD, next_path="/overview"):
    return client.post(
        "/login", data={"username": "admin", "password": password, "next": next_path}
    )


def test_apr1_matches_openssl():
    assert md5_crypt(PASSWORD.encode(), b"abcdefgh") == APR1
    assert verify_password(PASSWORD, APR1)
    assert not verify_password("wrong", APR1)
    try:
        ref = subprocess.run(
            ["openssl", "passwd", "-apr1", "-salt", "Xy", "x"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return
    assert md5_crypt(b"x", b"Xy") == ref


def test_page_without_session_redirects_to_login(client):
    r = client.get("/overview?tab=x")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Foverview%3Ftab%3Dx"


def test_api_without_session_answers_401_envelope(client):
    r = client.get("/api/v1/health/summary")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"
    assert client.post("/overview").status_code == 401


def test_open_paths_need_no_session(client):
    for path in ("/health", "/mcp/", "/static/app.css", "/public/blobs/abc"):
        assert client.get(path).status_code == 200, path
    # Page code is not part of the login page; it stays behind the gate.
    assert client.get("/static/js/pages/run.js").status_code == 303


def test_login_form_is_fillable_by_a_password_manager(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert 'autocomplete="username"' in r.text
    assert 'autocomplete="current-password"' in r.text


def test_login_sets_session_and_returns_to_next(client):
    r = _login(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/overview"
    cookie = r.headers["set-cookie"]
    assert COOKIE_NAME in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/overview").status_code == 200
    assert client.get("/api/v1/health/summary").status_code == 200


def test_wrong_password_is_refused(client):
    r = _login(client, password="nope")
    assert r.status_code == 401
    assert "Неверное имя или пароль" in r.text
    assert COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_next_cannot_leave_the_site(client):
    for evil in ("//evil.example/x", "https://evil.example/", "/\\evil.example"):
        assert _login(client, next_path=evil).headers["location"] == "/"


def test_logout_drops_the_session(client):
    _login(client)
    r = client.post("/logout")
    assert r.status_code == 303
    assert client.get("/overview").status_code == 303


def test_password_change_ends_old_sessions(tmp_path):
    settings = _settings(tmp_path)
    c = TestClient(_app(settings), follow_redirects=False)
    _login(c)
    assert c.get("/overview").status_code == 200
    path = tmp_path / "users"
    path.write_text("admin:" + md5_crypt(b"new", b"saltsalt") + "\n")
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    assert c.get("/overview").status_code == 303


def test_tampered_or_expired_cookie_is_refused(tmp_path):
    auth = WebAuth(_settings(tmp_path))
    token = auth.issue("admin", now=1000)
    assert auth.session_user(token, now=1001) == "admin"
    assert auth.session_user(token, now=1000 + auth.session_s + 1) is None
    name, exp, sig = token.split(".")
    assert auth.session_user(f"{name}.{int(exp) + 999}.{sig}", now=1001) is None
    assert auth.session_user("garbage", now=1001) is None


def test_secret_is_generated_once_and_reused(tmp_path):
    a = WebAuth(_settings(tmp_path))
    b = WebAuth(_settings(tmp_path))
    assert a.secret == b.secret
    assert (tmp_path / "web_session.key").stat().st_mode & 0o777 == 0o600


def test_failures_are_throttled(client):
    for _ in range(10):
        _login(client, password="nope")
    r = _login(client)
    assert r.status_code == 429


def test_loopback_without_proxy_headers_is_trusted(tmp_path):
    app = _app(_settings(tmp_path))
    direct = TestClient(app, client=("127.0.0.1", 50000), follow_redirects=False)
    assert direct.get("/api/v1/health/summary").status_code == 200
    proxied = direct.get("/api/v1/health/summary", headers={"X-Real-IP": "203.0.113.5"})
    assert proxied.status_code == 401


def test_loopback_trust_can_be_turned_off(tmp_path):
    app = _app(_settings(tmp_path, WEB_AUTH_TRUST_LOOPBACK=False))
    direct = TestClient(app, client=("127.0.0.1", 50000), follow_redirects=False)
    assert direct.get("/api/v1/health/summary").status_code == 401


def test_gate_is_off_without_htpasswd(tmp_path):
    settings = Settings(DB_PATH=str(tmp_path / "r.db"), WEB_AUTH_HTPASSWD=None)
    c = TestClient(_app(settings), follow_redirects=False)
    assert c.get("/overview").status_code == 200
    assert c.get("/login").status_code == 303
