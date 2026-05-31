"""Unit tests for the /mcp Bearer-token ASGI middleware.

Tested in isolation against a trivial ASGI app so we don't boot a second FastMCP
instance (its session manager can only run once per process).
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from router_mcp.app import McpBearerAuthMiddleware

TOKEN = "secret-token"


def _make_app(token: str | None) -> Starlette:
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/mcp", ok), Route("/health", ok), Route("/api/x", ok)])
    app.add_middleware(McpBearerAuthMiddleware, token=token)
    return app


def test_no_token_is_noop():
    client = TestClient(_make_app(None))
    assert client.get("/mcp").status_code == 200


def test_mcp_requires_bearer_when_token_set():
    client = TestClient(_make_app(TOKEN))
    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_non_mcp_paths_unaffected():
    client = TestClient(_make_app(TOKEN))
    assert client.get("/health").status_code == 200
    assert client.get("/api/x").status_code == 200
