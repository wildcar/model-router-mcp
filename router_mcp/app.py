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
from fastapi.staticfiles import StaticFiles

from router_mcp.bootstrap import build_core
from router_mcp.config import Settings, load_settings
from router_mcp.logging_config import setup_logging
from router_mcp.mcp_server.tools import mcp
from router_mcp.runtime import set_core
from router_mcp.web.api import api_router, blob_router
from router_mcp.web.pages import pages_router

logger = logging.getLogger(__name__)

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

    # MCP Streamable HTTP endpoint (POST/GET to /mcp).
    app.mount("/mcp", mcp_app)

    # Web JSON API + blob serving + pages.
    app.include_router(api_router, prefix="/api")
    app.include_router(blob_router)
    app.include_router(pages_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
