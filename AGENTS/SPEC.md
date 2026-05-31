# model-router-mcp — functional & technical specification

Source of truth for *what the product does* and *how it is built*. Keep in sync
with reality (update before code when the contract changes).

## Purpose

An MCP server that routes a request to an appropriate AI model and returns the
response, across multiple modalities (chat, image, speech-to-text, text-to-speech,
CLI) and multiple providers. It maintains a registry of known/connected models
(service-provider → model-provider → model) with cost tiers, logs every request
and response keyed by an *external* user id (users are managed elsewhere; only the
id arrives, for per-user statistics), and computes per-request cost from registry
prices (with a hook for monthly-subscription amortization). Besides HTTP/API
providers it can invoke `claude`/`codex` via CLI. A web console allows testing the
service and administering the registry.

## Stack

- Python 3.11+ (developed/tested on 3.12).
- MCP: official `mcp` SDK (FastMCP) over **Streamable HTTP**.
- Web/server: FastAPI + uvicorn; Jinja2 templates + vanilla JS for the UI.
- HTTP client: httpx (async). Config: pydantic-settings. Storage: aiosqlite (raw SQL).
- Deployment target: a single long-running ASGI process.

## Architecture

```
MCP client ─┐                         ┌─ adapters/* (one per provider)
            ├─→ FastAPI app ──────────┤    OpenRouter (chat+image) [Phase 1]
Web browser ┘   ├─ /mcp  (FastMCP Streamable HTTP, tools)
                ├─ /api/* (JSON API for the web UI)
                ├─ /, /registry, /logs (Jinja2 pages)
                └─ /blobs/{ref} (binary payload serving)
                         │
                         ▼
              ModelRouter.dispatch ──→ RegistryService (catalog+manual merge)
                         │              accounting.compute_cost
                         ▼
                  Database (aiosqlite): service_providers, registry_models,
                  subscriptions, request_logs   +   BlobStore (disk)
```

The `domain/` package holds pure types (Modality, RouterRequest/Response, Usage,
Attachment, errors). Adapters, routing, accounting, mcp and web all depend inward
on it; adapters never touch storage/mcp directly.

## Functional requirements

1. ✅ Normalized provider-adapter interface (`generate(request, model)`), modality-aware.
2. ✅ Model registry with 3-level mapping, cost tiers (free/cheap/expensive), manual + catalog rows.
3. ✅ OpenRouter live catalog merge into the registry (TTL-cached).
4. ✅ Routing: explicit `model_id`, or modality + tier/provider hints, or per-modality default.
5. ✅ Per-request/response logging by `external_user_id` with token usage and cost.
6. ✅ Cost computation from registry prices; amortized-subscription hook (table + field).
7. ✅ MCP tools over Streamable HTTP: `list_models`, `chat`, `get_usage`.
8. ✅ Web UI: test console + registry admin (providers, manual models) + logs/usage.
9. ⏳ Modality breadth: image (Codex OAuth), STT (Deepgram), TTS (ElevenLabs). [Phase 2]
10. ⏳ Direct adapters: DeepSeek, Claude API. [Phase 3]
11. ⏳ CLI engines: `codex` (CodexRunner), `claude`. [Phase 4]
12. ⏳ Full billing reports / amortization rollups. [later]

## Project structure

```
router_mcp/
  config.py            pydantic-settings Settings
  app.py               build_app(): FastAPI + FastMCP mount + lifespan
  main.py              uvicorn entrypoint (module-level `app`)
  bootstrap.py         build_core(): wires DB, adapters, registry, router
  runtime.py           process-wide AppCore handle for MCP tools
  logging_config.py
  domain/              modality, messages, errors (pure types)
  adapters/            base (ABC + RetryClientMixin), openrouter
  registry/            models (RegistryModel), service (catalog merge)
  routing/router.py    ModelRouter.resolve + dispatch
  accounting/cost.py   compute_cost
  storage/database.py  aiosqlite schema + CRUD + logging + usage
  blobs/store.py       on-disk blob store
  mcp_server/          tools (FastMCP), serialize
  web/                 api (JSON), pages (Jinja2), templates/, static/
tests/                 test_smoke.py (chain), test_web.py (app), mcp_probe.py (manual)
```

## Deployment

- Run: `uvicorn router_mcp.main:app --host $HOST --port $PORT` (or `python -m router_mcp.main`).
- Secrets/config live in `.env` at repo root (see `.env.example`); never committed.
- Runtime data (`data/router.db`, `data/blobs/`) and `logs/` are gitignored.
- MCP endpoint: `http://<host>:<port>/mcp` (Streamable HTTP). Web UI at `/`.

## Current state

- ✅ Phase 0 (skeleton) + Phase 1 (OpenRouter chat end-to-end) complete and tested.
- ⏳ Phases 2–6 per the plan: more modalities, direct/CLI adapters, hardening.

See `AGENTS/STATE.md` for the live Now / Next snapshot.

## Data sources & dependencies

- OpenRouter `/models` + `/chat/completions` (requires `OPENROUTER_API_KEY`).
- Reused connection patterns originate in sibling repos: `ort_bot` (OpenRouter,
  Deepgram, ElevenLabs, Codex-image, model cache, DB/config patterns),
  `telecodex_bot` (Codex CLI runner), `openai-auth-sync` (OAuth token sync).
- Codex OAuth shares `auth.json` with those repos — use a dedicated copy to avoid
  refresh-token rotation conflicts.
