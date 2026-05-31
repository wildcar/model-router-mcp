# ADR-0004: Deployment — systemd service behind nginx/TLS, manual deploy.sh

**Status:** Accepted
**Date:** 2026-05-31
**Deciders:** project owner + Claude
**Scope:** `deploy/*`, `router_mcp/app.py` (health + auth middleware), `router_mcp/config.py`

---

## Context

model-router-mcp must run as a managed service on this host: survive crashes and
reboots, be reachable by MCP clients over HTTPS, and be updatable on demand. The
host already runs several Python services under systemd; conventions to mirror:
`Restart=always`, bind `127.0.0.1`, `EnvironmentFile=.env`, hardened units under
dedicated users (`movie-*`), and a git-fetch/reset + `systemctl restart` update
script precedent (`movie-handler-clients/update.sh`).

## Decision

- **Process wrapper:** a hardened systemd unit `model-router-mcp.service` running
  `uvicorn router_mcp.main:app --host 127.0.0.1 --port 8088 --workers 1` as a
  dedicated system user `modelrouter`, from `/opt/model-router-mcp`. `Restart=always`,
  `RestartSec=5`, `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`,
  `ReadWritePaths=.../data .../logs`. Single worker because the app holds in-memory
  state (AppCore, one aiosqlite connection, one FastMCP session manager — ADR-0003).
- **Edge:** nginx reverse-proxy + TLS (certbot) on a subdomain. `/mcp` is proxied
  with `proxy_buffering off` + `proxy_http_version 1.1` (Streamable HTTP/SSE safe);
  the web console/API/blobs sit behind nginx HTTP Basic auth; `/health` is open.
- **Auth:** `/mcp` additionally requires `Authorization: Bearer $AUTH_TOKEN` when
  set, enforced by a **pure-ASGI** middleware (not BaseHTTPMiddleware, which would
  buffer SSE). Defence-in-depth: token for programmatic MCP clients, Basic auth for
  the browser console.
- **Health:** unauthenticated `GET /health` → `{status, version, providers}` for
  nginx/monitoring and the post-restart probe in `deploy.sh`.
- **Updates:** manual `deploy/deploy.sh` (run by `keeper`): pull `origin/main` as
  `modelrouter` (read-only deploy key), reinstall deps, `systemctl restart`, wait
  for `/health`. No timer/inotify/webhook for now (deferred).
- **Private-repo access for the dedicated user:** a read-only GitHub deploy key for
  `modelrouter`, so it can `git pull` without `keeper`'s credentials.

## Consequences

- Crash/reboot resilience and HTTPS exposure with minimal moving parts.
- One-command updates; rollback is `git reset` to a prior SHA + re-run.
- Single worker caps throughput (acceptable for now; noted in ADR-0003).
- The dedicated user cannot see `keeper`'s Codex/Claude OAuth — must be addressed
  when CLI engines (Phase 4) land (dedicated auth copy or run under keeper).
- Adding automated CD later is additive (a `.timer`/`.path` calling `deploy.sh`).

## Alternatives considered

- **Run from `/home/keeper/repo` as `keeper`** (like `ort_bot`) — simpler and keeps
  CLI OAuth access, but less isolation; the owner chose a dedicated hardened user.
- **BaseHTTPMiddleware for auth** — buffers streamed responses and would break
  Streamable HTTP/SSE; rejected for a pure-ASGI gate.
- **GitHub webhook / git-poll timer for auto-update** — deferred; manual `deploy.sh`
  chosen for now to avoid exposed endpoints / polling complexity.
