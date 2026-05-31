"""FastAPI application: mounts the MCP Streamable-HTTP app and the web UI.

Lifespan chaining is the critical detail: FastMCP's streamable-http app runs a
session manager that MUST be active for /mcp to work. We enter
`mcp.session_manager.run()` inside our own lifespan (after building the core),
and tear the core down on shutdown.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from router_mcp import __version__
from router_mcp.bootstrap import build_core
from router_mcp.config import Settings, load_settings
from router_mcp.logging_config import setup_logging
from router_mcp.mcp_server.tools import mcp
from router_mcp.runtime import get_core, set_core
from router_mcp.web.api import api_router, blob_router
from router_mcp.web.pages import pages_router

logger = logging.getLogger(__name__)


class McpBearerAuthMiddleware:
    """Pure-ASGI gate: require `Authorization: Bearer <token>` on /mcp.

    Implemented as raw ASGI (not BaseHTTPMiddleware) so it never buffers the
    streamable-HTTP/SSE response body — valid requests pass through untouched.
    The web console/API is guarded by nginx Basic auth; /health stays open. When
    no token is configured (local dev) this is a no-op.
    """

    def __init__(self, app, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if self.token and scope.get("type") == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                headers = dict(scope.get("headers") or [])
                auth = headers.get(b"authorization", b"").decode()
                scheme, _, value = auth.partition(" ")
                if scheme.lower() != "bearer" or value.strip() != self.token:
                    response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_file, settings.log_level)

    # Build the streamable-http ASGI app once; this also instantiates the
    # session manager that the lifespan below must run.
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        core = await build_core(settings)
        set_core(core)
        logger.info(
            "Core ready: providers=%s db=%s", list(core.adapters), settings.db_path
        )
        async with mcp.session_manager.run():
            yield
        await core.aclose()

    app = FastAPI(title="model-router-mcp", lifespan=lifespan)

    # Bearer-token gate for /mcp (no-op when AUTH_TOKEN is unset).
    app.add_middleware(McpBearerAuthMiddleware, token=settings.auth_token)

    @app.get("/health")
    async def health() -> dict:
        try:
            providers = sorted(get_core().adapters)
        except RuntimeError:
            providers = []
        return {"status": "ok", "version": __version__, "providers": providers}

    # MCP Streamable HTTP endpoint (POST/GET to /mcp).
    app.mount("/mcp", mcp_app)

    # Web JSON API + blob serving + pages.
    app.include_router(api_router, prefix="/api")
    app.include_router(blob_router)
    app.include_router(pages_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
