"""Routing: map a request to a provider+model and execute it with logging."""

from router_mcp.routing.router import DispatchResult, ModelRouter

__all__ = ["ModelRouter", "DispatchResult"]
