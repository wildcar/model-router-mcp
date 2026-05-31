"""Uvicorn entrypoint.

Run with:  python -m router_mcp.main   (or)   uvicorn router_mcp.app:app
"""

from __future__ import annotations

import uvicorn

from router_mcp.app import build_app
from router_mcp.config import load_settings

# Module-level app so `uvicorn router_mcp.main:app` works too.
settings = load_settings()
app = build_app(settings)


def run() -> None:
    uvicorn.run(
        "router_mcp.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
