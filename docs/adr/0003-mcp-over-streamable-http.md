# ADR-0003: MCP over Streamable HTTP mounted on FastAPI

**Status:** Accepted
**Date:** 2026-05-31
**Deciders:** project owner + Claude
**Scope:** `router_mcp/app.py`, `router_mcp/mcp_server/*`, `router_mcp/runtime.py`

---

## Context

The server must expose an MCP interface AND a web test/admin console, support a
multi-user accounting model where an external user id is supplied per call, and be
network-reachable. The MCP Python SDK (FastMCP) offers stdio and Streamable HTTP
transports.

## Decision

Use FastMCP's **Streamable HTTP** app, mounted on a FastAPI app that also serves
the JSON API and Jinja2 pages, in one process.

- `FastMCP("model-router", stateless_http=True, streamable_http_path="/")` so that
  mounting the sub-app at `/mcp` yields the endpoint at `/mcp` (not `/mcp/mcp`).
- The FastAPI lifespan builds the app core (DB, adapters, registry, router) once,
  publishes it via `runtime.set_core`, then enters `mcp.session_manager.run()`
  before yielding, and tears the core down on shutdown. Running the session manager
  is mandatory — without it `/mcp` fails ("Session terminated").
- MCP tool functions reach the core through `runtime.get_core()` (the tools are
  registered at import time on a module-level FastMCP instance; the core is wired
  at startup). The web API shares the same core.
- `external_user_id` is an explicit tool/endpoint argument, not derived from
  transport auth: MCP clients are themselves the "user", so the end-user id is
  application data the caller forwards.

## Consequences

- One deployable process serves MCP + UI + blobs; UI and MCP share one routing
  core and DB.
- The FastMCP session manager can be run only once per instance per process — so
  there is one app per process. Tests that need the web app use a module-scoped
  client fixture to respect this.
- stdio is not exposed in v1; can be added later from the same tools module.

## Alternatives considered

- **stdio transport** — local-only, no network reach, and a separate process would
  be needed to host the web test page; rejected given the multi-user/web goals.
- **Two separate processes (MCP and web)** — duplicates the routing core/DB wiring
  and complicates shared state; rejected for v1.
