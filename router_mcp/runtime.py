"""Process-wide handle to the assembled application core.

FastMCP tool functions and the web API both need the same Database / Registry
service / ModelRouter instances. They are built once at startup (see bootstrap)
and published here so tool functions — which FastMCP calls without our wiring —
can reach them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from router_mcp.bootstrap import AppCore

_core: "AppCore | None" = None


def set_core(core: "AppCore") -> None:
    global _core
    _core = core


def get_core() -> "AppCore":
    if _core is None:
        raise RuntimeError("Application core is not initialized")
    return _core
