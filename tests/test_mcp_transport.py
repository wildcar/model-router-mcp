"""Unit tests for the /mcp transport middlewares (trailing slash, body drain).

Tested in isolation against a trivial ASGI app so we don't boot a second FastMCP
instance (its session manager can only run once per process). The failure mode
they guard against — a 307/401 sent before the body is read escalating to a TCP
RST — needs real sockets to show up, so what we pin here is the observable
contract: no redirect on slash-less /mcp, and the body fully consumed before an
early 401.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from router_mcp.app import McpBearerAuthMiddleware, McpTrailingSlashMiddleware

TOKEN = "secret-token"


def _make_app(token: str | None = None) -> Starlette:
    """Mirror build_app's shape: a sub-app with route "/" mounted at /mcp."""

    async def echo(request):
        body = await request.body()
        return JSONResponse({"path": request.url.path, "body_len": len(body)})

    sub = Starlette(routes=[Route("/", echo, methods=["GET", "POST"])])
    app = Starlette(routes=[Mount("/mcp", app=sub), Route("/health", echo)])
    app.add_middleware(McpBearerAuthMiddleware, token=token)
    app.add_middleware(McpTrailingSlashMiddleware)
    return app


def test_slashless_mcp_is_served_not_redirected():
    client = TestClient(_make_app())
    response = client.post("/mcp", content=b"x" * 1024, follow_redirects=False)
    assert response.status_code == 200
    assert response.json() == {"path": "/mcp/", "body_len": 1024}


def test_trailing_slash_path_still_served():
    client = TestClient(_make_app())
    response = client.post("/mcp/", content=b"x", follow_redirects=False)
    assert response.status_code == 200


def test_other_paths_not_rewritten():
    client = TestClient(_make_app())
    assert client.get("/health", follow_redirects=False).json()["path"] == "/health"


def test_rewrite_applies_before_auth_check():
    client = TestClient(_make_app(TOKEN))
    response = client.post(
        "/mcp",
        content=b"x",
        headers={"Authorization": f"Bearer {TOKEN}"},
        follow_redirects=False,
    )
    assert response.status_code == 200


def test_unauthorized_body_is_drained_before_401():
    """The 401 must arrive after the body is consumed, not instead of it.

    TestClient can't observe a TCP RST, so we assert the drain directly: every
    http.request message was pulled off the channel before the response went out.
    """
    drained = []

    async def app(scope, receive, send):  # pragma: no cover - never reached
        raise AssertionError("unauthorized request must not reach the app")

    gate = McpBearerAuthMiddleware(app, token=TOKEN)

    chunks = [b"a" * 1024, b"b" * 1024, b""]

    async def receive():
        chunk = chunks[len(drained)]
        drained.append(chunk)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": len(drained) < len(chunks),
        }

    sent = []

    async def send(message):
        sent.append(message)

    import asyncio

    scope = {"type": "http", "path": "/mcp/", "headers": [], "method": "POST"}
    asyncio.run(gate(scope, receive, send))

    assert len(drained) == len(chunks)
    assert sent[0]["status"] == 401
