"""FastAPI application: mounts the MCP Streamable-HTTP app and the web UI.

Lifespan chaining is the critical detail: FastMCP's streamable-http app runs a
session manager that MUST be active for /mcp to work. We enter
`mcp.session_manager.run()` inside our own lifespan (after building the core),
and tear the core down on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from router_mcp.bootstrap import build_core
from router_mcp.config import Settings, load_settings
from router_mcp.domain.errors import RouterError
from router_mcp.logging_config import setup_logging
from router_mcp.mcp_server.tools import mcp
from router_mcp.runtime import set_core
from router_mcp.web import retention
from router_mcp.web.api import api_router, blob_router
from router_mcp.web.pages import pages_router
from router_mcp.web.views import error_envelope

logger = logging.getLogger(__name__)


async def _drain_request_body(receive) -> None:
    """Consume the request body before answering without it.

    Responding while the body is still in flight makes uvicorn close a
    connection with unread data, which the kernel escalates to a TCP RST — the
    client then sees ECONNRESET instead of our status code. Any early response
    on /mcp must drain first.
    """
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body"):
            return


class McpTrailingSlashMiddleware:
    """Rewrite `/mcp` to `/mcp/` in the scope instead of letting the mounted
    sub-app answer with redirect_slashes' 307.

    That 307 was sent before the request body was read, so uvicorn closed the
    connection on an unconsumed body and the kernel sent a TCP RST. Callers
    POSTing megabyte-scale bodies to /mcp got an instant ECONNRESET — sometimes
    at ~1.5 MB (a race between reading the 307 and the RST), always past the
    ~4 MB that tcp_wmem lets the client buffer. Serving the path directly
    removes the redirect and with it the whole failure mode.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope["path"] = "/mcp/"
            raw_path = scope.get("raw_path")
            if raw_path is not None:
                scope["raw_path"] = raw_path + b"/"
        await self.app(scope, receive, send)


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
                    await _drain_request_body(receive)
                    response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class RevalidatedStaticFiles(StaticFiles):
    """Static files the browser re-checks on every use.

    `?v=` on the template links busts only the entry module; the imports inside
    it (`../core/api.js`) carry no version, and heuristic caching let browsers
    run month-old halves of the client against a fresh other half. `no-cache`
    means revalidate, not refetch: an unchanged file is one conditional request
    answering 304.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


async def catalog_sync_loop(core, interval_seconds: int) -> None:
    """Re-read every provider's model list on a timer.

    Providers retire model ids without warning; without this the registry only
    learns about it when a request fails. Failures inside a sync are already
    swallowed per provider, so this loop only guards against unexpected ones.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await core.registry.sync_all(force=True)
        except Exception:  # pragma: no cover - the timer must survive anything
            logger.warning("Scheduled catalog sync failed", exc_info=True)


async def retention_sweep_loop(core, interval_seconds: int) -> None:
    """Apply the retention policy on a timer.

    With both retentions left at 0 this does nothing at all, which is how an
    installation that never configured a policy keeps behaving.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        if not (core.settings.log_retention_days or core.settings.blob_retention_days):
            continue
        try:
            await retention.purge(core)
        except Exception:  # pragma: no cover - the timer must survive anything
            logger.warning("Scheduled retention purge failed", exc_info=True)


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
        tasks: list[asyncio.Task] = []
        if settings.catalog_sync_interval_seconds > 0:
            tasks.append(
                asyncio.create_task(
                    catalog_sync_loop(core, settings.catalog_sync_interval_seconds)
                )
            )
        if settings.retention_sweep_interval_seconds > 0:
            tasks.append(
                asyncio.create_task(
                    retention_sweep_loop(core, settings.retention_sweep_interval_seconds)
                )
            )
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await core.aclose()

    app = FastAPI(title="model-router-mcp", lifespan=lifespan)

    # Bearer-token gate for /mcp (no-op when AUTH_TOKEN is unset).
    app.add_middleware(McpBearerAuthMiddleware, token=settings.auth_token)
    # Outermost, so nothing ever sees a slash-less /mcp and redirects it.
    app.add_middleware(McpTrailingSlashMiddleware)

    @app.exception_handler(RouterError)
    async def router_error_handler(request: Request, exc: RouterError) -> JSONResponse:
        """One error shape for the whole API (see web/views.error_envelope).

        Each error class carries its own HTTP status and stable code, so a
        caller can branch on `code` and the console can offer the right next
        action — including "open this failure in the log", which needs the
        `log_id` the router attached on the way out.
        """
        logger.info(
            "API error %s %s -> %s (%s)",
            request.method, request.url.path, exc.code, exc,
        )
        return JSONResponse(error_envelope(exc), status_code=exc.http_status)

    @app.get("/health")
    async def health() -> dict:
        """Public liveness only.

        The version and the provider inventory used to be here; that is a map of
        the installation handed out before Basic auth. The console reads the
        same facts from /api/v1/health/summary, behind the gate.
        """
        return {"status": "ok"}

    # MCP Streamable HTTP endpoint (POST/GET to /mcp).
    app.mount("/mcp", mcp_app)

    # Web JSON API + blob serving + pages. `/api/v1` is the contract; the bare
    # `/api` prefix stays so an in-flight page from before a restart does not
    # break mid-session.
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(api_router, prefix="/api", include_in_schema=False)
    app.include_router(blob_router)
    app.include_router(pages_router)
    app.mount("/static", RevalidatedStaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
