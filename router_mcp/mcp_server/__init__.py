"""MCP server surface (FastMCP tools). Named `mcp_server` to avoid shadowing the
installed `mcp` SDK package."""

from router_mcp.mcp_server.tools import mcp

__all__ = ["mcp"]
