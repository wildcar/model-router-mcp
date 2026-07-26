"""What the MCP side of this service looks like from outside.

MCP is the primary interface here and the console is the secondary one, which
made it odd that the console had nothing to say about it (§12.2). This answers
the three questions an operator actually has: what tools does the endpoint
expose and with what schemas, does it answer at all, and who has been calling
it.

There is deliberately no "MCP sessions" screen. The server is stateless by
ADR-0003, so such a screen would be empty by construction.
"""

from __future__ import annotations

import time
from typing import Any

from router_mcp.mcp_server.tools import mcp


async def tool_inventory() -> list[dict[str, Any]]:
    """Tools as the client sees them: name, description, input schema."""
    tools = await mcp.list_tools()
    return [
        {
            "name": tool.name,
            "description": (tool.description or "").strip(),
            "input_schema": tool.inputSchema,
        }
        for tool in tools
    ]


async def summary(core, *, hours: int = 24) -> dict[str, Any]:
    """The inventory joined with what the log knows about real calls."""
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    tools = await tool_inventory()
    stats = await core.db.get_tool_stats(since=since)
    clients = await core.db.get_client_stats(since=since)

    by_name = {row["tool"]: row for row in stats}
    for tool in tools:
        row = by_name.get(tool["name"], {})
        tool["calls"] = row.get("calls", 0)
        tool["errors"] = row.get("errors", 0)
        tool["p50_ms"] = row.get("p50_ms")

    # Calls recorded against a tool this build no longer exposes: an old client
    # is still asking for something that was renamed or removed.
    known = {tool["name"] for tool in tools}
    unknown = [row for row in stats if row["tool"] not in known]

    return {
        "endpoint": "/mcp",
        "stateless": True,
        "auth_required": bool(core.settings.auth_token),
        "tools": tools,
        "retired_tools_called": unknown,
        "clients": clients,
        "window_hours": hours,
    }


async def probe() -> dict[str, Any]:
    """Self-check: can this process enumerate its own tools right now?

    In-process on purpose. An HTTP round trip through nginx would mostly test
    nginx, and the console is already on the other side of it.
    """
    started = time.monotonic()
    try:
        tools = await mcp.list_tools()
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "status": "error",
            "detail": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "status": "ok",
        "detail": f"инструментов {len(tools)}, режим без сохранения состояния",
        "tools": len(tools),
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
