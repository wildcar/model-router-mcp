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
MCP client ─┐                         ┌─ adapters/* (one per provider):
            ├─→ FastAPI app ──────────┤    openrouter (chat+image), deepseek &
Web browser ┘   ├─ /mcp  (FastMCP …)  │    claude_api (chat), codex_image (image),
                │                     │    deepgram (stt), elevenlabs (tts),
                │                     └    codex_cli & claude_cli (cli)
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
7. ✅ MCP tools over Streamable HTTP: `list_models`, `chat`, `generate_image`, `transcribe`, `synthesize_speech`, `run_cli`, `get_usage`.
8. ✅ Web UI: test console (chat/image/stt/tts) + registry admin (providers: `enabled` admin toggle + live `configured` state, manual models) + logs/usage.
9. ✅ Modality breadth: image (Codex OAuth), STT (Deepgram), TTS (ElevenLabs). [Phase 2]
10. ✅ Direct adapters: DeepSeek (OpenAI-compatible), Claude API (Anthropic Messages). [Phase 3]
11. ✅ CLI engines: `codex` (`codex exec --json`) and `claude` (`claude -p --output-format json`), via `run_cli` with a cwd whitelist and a configurable default cwd (`CLI_DEFAULT_CWD`). [Phase 4]
12. ⏳ Full billing reports / amortization rollups. [later]
13. ✅ Production deploy: systemd unit (auto-restart), nginx+TLS, `/health`, `/mcp` Bearer auth, manual `deploy.sh`.
14. ✅ Truncation is a distinct, self-explaining failure (`ResponseTruncated`), never "empty response"; `finish_reason` is logged.
15. ✅ Per-provider catalog sync on a period: adapters expose `list_catalog()`, models missing upstream are deprecated rather than deleted, seeds never resurrect a retired id.
16. ✅ Price provenance per model (`price_source`, `price_source_kind`, `price_checked_at`) and per provider (`pricing_url`); unverified prices surface as `cost_source='estimated'`.
17. ✅ Request-level fallback policy for dead/unavailable models (`same` / `cheaper` / `pricier`), with the substitution reported and logged.
18. ✅ Web contract: `/api/v1`, the `Run` envelope, the typed error envelope, `{items, total, next_cursor, facets}` lists, cursor-paged and column-projected log. [console wave 0]
19. ✅ Call provenance and execution detail in the log: channel/tool/client, HTTP retries, upstream status and request id, token breakdown (cache/reasoning), `finished_at`.
20. ✅ Response files are served safely: MIME whitelist for inline rendering, `nosniff`, immutable cache, byte ranges.
21. ✅ Capabilities: the parameter matrix is derived from adapters (`SUPPORTED_PARAMS`), served with the glossary and the server-fixed settings. [console wave 1]
22. ✅ Dry run: `POST /api/v1/plan` explains what would happen without touching the network, and answers even when nothing can run.
23. ✅ Console screens: five-item navigation, the «Прогон» form built from capabilities, one inspector with a two-level timeline over the `Run` envelope, partial price editing with provenance-based protection.

## Catalog freshness, price provenance, fallback

Contract for requirements 14–17.

### Failure taxonomy (14)

`ResponseTruncated(ProviderError)` is raised when a provider returns no usable
content *and* reports that generation hit the output-token ceiling
(`finish_reason == "length"` / `stop_reason == "max_tokens"`). The message names
the ceiling and the tokens actually produced, so the caller knows to raise
`max_tokens` instead of debugging the prompt. `request_logs.finish_reason` keeps
the provider's stop reason for both successful and failed calls.

### Catalog sync (15)

- `ProviderAdapter.list_catalog()` returns `list[RegistryModel] | None`; `None`
  means "this provider has no queryable catalog" (CLI engines, TTS/STT).
- `RegistryService.sync_provider(provider_id)` / `sync_all()` reconcile one or all
  providers; both are TTL-guarded (`MODEL_CACHE_TTL_SECONDS`) and force-able.
  A background task in the app lifespan re-syncs every
  `CATALOG_SYNC_INTERVAL_SECONDS` (0 disables); `POST /api/registry/refresh`
  triggers it by hand.
- Reconciliation rules:
  - seen upstream → `last_seen_at = now`, `is_active = 1`, `deprecated_at = NULL`;
  - absent upstream → `is_active = 0`, `deprecated_at = now`. Rows are never
    deleted: logs and cost history reference them;
  - prices from a catalog that carries pricing (OpenRouter) overwrite rows whose
    `source = 'catalog'`; rows with `source = 'manual'` keep their prices,
    display name and pricing mode;
  - a catalog without pricing (DeepSeek, Anthropic) never zeroes an existing price.
- Bootstrap seeds are insert-if-absent, so a retired model id cannot be
  resurrected by a restart.

### Price provenance (16)

`registry_models` gains `price_source` (URL or `api:<endpoint>`),
`price_source_kind` (`api` | `page` | `manual` | `estimate`) and
`price_checked_at`; `service_providers` gains `pricing_url`. Where a provider
publishes prices through its API (OpenRouter `/models`) the kind is `api`;
elsewhere the row points at the vendor's price page and stays `page`/`manual`
until a human confirms it. `compute_cost` reports `cost_source='estimated'`
instead of `'computed'` when the price behind the number is unverified
(`kind='estimate'`), so an invented figure is visible in logs and usage.

### Fallback policy (17)

Requests may carry `fallback = {policy, max_steps, scope}`:

- `policy`: `off` (default, current behavior) | `same` | `cheaper` | `pricier`;
- `max_steps`: how many substitutions to try (default 1);
- `scope`: `provider` (default, same service provider) | `vendor` (same
  model_provider anywhere) | `any`.

Candidates are the active models of the same modality inside the scope, ordered
by `prompt_price + completion_price`; `same` takes the nearest neighbour,
`cheaper`/`pricier` step down/up that ladder. Fallback triggers on: unknown
model id, `is_active = 0`, provider disabled or unconfigured, and provider errors
that mean "this model cannot serve the request" (model not found, unavailable).
It never crosses modality and never retries a prompt-level rejection. The
substitution is explicit in the response (`requested_model_id`, `model_id`,
`fallback_reason`) and in `request_logs`.

## Web contract (18–20)

Wave 0 of `docs/UI_CONCEPT_v2.md`. The console is one consumer of it; MCP and
any other caller see the same shapes.

### Prefix and envelopes

- `/api/v1` is the contract. The bare `/api` prefix serves the same router so a
  page loaded before a restart keeps working; new work targets `/api/v1`.
- Lists answer `{items, total, next_cursor, facets}` — a bare array has nowhere
  to carry the total, the cursor or the facet counts.
- Money is never returned without `cost_source`.
- `/health` is public and says `{"status": "ok"}` only. Version and provider
  inventory are a map of the installation and move behind the auth gate.

### The `Run` envelope

One shape for a run whether it just executed or is read back from the log, built
by `web/views.run_from_dispatch` and `web/views.run_from_log`;
`tests/test_web_views.py` fails if their key sets diverge. Fields: `run_id`,
`log_id`, `status`, `modality`, `channel`, `tool`, `client_id`,
`model {provider, model_id, display_name}`, `requested_model`,
`fallback {kind, trigger, reason}`, `usage {prompt, completion, total,
cache_read, cache_write, reasoning, audio_seconds, images, characters, raw}`,
`cost {value, source, breakdown[]}`, `latency_ms`, `attempts`,
`provider_status`, `provider_request_id`, `finish_reason`, `created_at`,
`finished_at`, `text`, `steps[]`, `attachments[]`, `ignored_params[]`, `error`.

A model is addressed by the pair (service provider + model id), never by the id
alone. `fallback.kind` distinguishes a substitution planned before the call
(`planned`) from one that followed a failure (`after_failure`): they cost
differently. `steps[]` is the two-level timeline — provider attempts, each with
the HTTP retries inside it, their upstream status and the wait the provider
asked for.

### Error envelope

Every failure is a `RouterError` subclass carrying `code`, `http_status`,
`retriable` and, where useful, `provider`, `model_id`, `provider_status`,
`retry_after_s` and a `hint`. One app-level handler renders
`{"error": {type, code, message, provider, model_id, log_id, retriable,
provider_status, retry_after_s, hint}}`.

| Code | Status | Raised when |
|---|---|---|
| `bad_request` | 400 | caller sent a malformed value |
| `not_found` | 404 | log entry or blob does not exist |
| `unknown_model` | 404 | no registry row for the model id |
| `model_retired` / `provider_disabled` / `model_unavailable` | 409 | the pair cannot serve the request |
| `response_truncated` | 422 | output ceiling hit before any content |
| `rate_limited` | 429 | provider refused; `retry_after_s` when it said so |
| `not_configured` | 503 | no key/binary; `hint.env_var` names the variable |
| `provider_timeout` | 504 | no answer inside the read timeout |
| `provider_error` / `provider_unavailable` / `access_denied` | 502 | upstream failure |

`log_id` is written before the exception leaves the router and travels on the
exception, so "open this failure in the log" works from every screen. MCP tools
get the same facts appended to the message text, since FastMCP hands clients
`str(exception)`.

### Log reads

`GET /api/v1/logs` is cursor-paged by descending id (offset would repeat rows,
because the log grows from the head) and projected to the columns the table
shows — the full row carries the response text and the raw usage block.
Filters: user, status, modality, model, channel, since/until, `with_fallback`,
`with_retries`, `unverified_cost`. `GET /api/v1/logs/{id}` returns the full row
as a `Run`.

### Adapter parameter declarations

`ProviderAdapter.SUPPORTED_PARAMS` maps modality → the `request.params` keys the
adapter reads. The adapter builds its payload from that same tuple, and anything
the caller sent outside it comes back as `ignored_params` on the run: silently
dropping `tools` or `seed` lets a caller pay for a request that ignored half of
what they asked for.

### Response files

Blobs render inline only for `image/*` and `audio/*`; everything else is
`Content-Disposition: attachment`. Always `X-Content-Type-Options: nosniff`,
immutable long cache (the ref is generated per payload) and byte-range support
so an audio player can seek.

## Console contract (21–23)

Wave 1 of `docs/UI_CONCEPT_v2.md`.

### Capabilities

`GET /api/v1/capabilities` answers with `providers`, `params`, `server`,
`labels` and a `schema_version`. `params` is keyed modality → provider and comes
from `ProviderAdapter.SUPPORTED_PARAMS` (see the ADR-0001 addendum), with
per-provider `model_overrides` for what a pair cannot express. `server` lists
what the caller cannot change — timeouts, retries, allowed cwd roots — as
derived facts plus the *name* of the environment variable, never a value or a
path. `labels` is the glossary from `web/labels.py`: one word per concept, in
one file, each with the visible explanation that belongs under a control rather
than in a tooltip.

### Dry run

`POST /api/v1/plan` returns the requested pair, the problem with it if any, the
policy in words, the candidate ladder with a verdict and a reason per model, the
pair that would be called, and a cost estimate that states its assumption. Two
invariants: it never reaches the network (`RegistryService.get_models` grew
`allow_sync=False`), and it answers 200 even when nothing can run, because
"почему запрос не ушёл" is the case the screen exists for.

### Registry edits

`GET /api/v1/registry/tree` builds the picker cascade (provider → vendor →
family → variant, with counts and prices) server-side; splitting `model_id` in
the browser guesses at a structure the database stores in its own column.

`PATCH /api/v1/registry/models/{provider}/{model_id}` writes only the columns it
is given, out of a closed set that excludes the pair, `source`, `is_active` and
the catalog dates. Prices arrive as USD per million and are stored per token.
Concurrency is guarded by a `revision` counter, not by `updated_at`: a
second-resolution timestamp cannot tell two edits inside one second apart.

Price protection is keyed on the origin of the **price**, not of the row. A
number a human vouched for (`price_source_kind` of `manual` or `page`) survives
synchronisation while the row stays a catalog row and keeps receiving name,
context and modality updates. `revert_price` hands it back to the catalog.

### Screens

Five: Обзор · Прогон · Модели · Журнал · Подключения. Filters, search, page,
sorting, the selected row, the active tab and `?log=` live in the address; the
live tail, collapsed nodes, the prompt draft and the theme do not.

The «Прогон» form is generated from capabilities, so a field is present exactly
when the adapter behind the chosen pair reads it. Model choice has two modes
(Авто with the server's reason line, and Выбрать самому with the server-built
cascade plus a `model_id` field that reports found / retired / provider-off as
it is typed). Fallback is one select in plain words, with `max_steps` under
"ещё настройки". The plan is recomputed 300 ms after any discrete change, and
the send button carries the estimate.

One inspector component serves «Прогон» and «Журнал», over the single `Run`
envelope, with four tabs — Ответ, Шаги, JSON, Сырьё. Its timeline has two
levels: provider attempts, and the HTTP retries inside each one with the wait
the provider asked for.

Frontend stack: native ES modules, no htmx, no Alpine, no build step — see
ADR-0005, which supersedes §18 of the concept.

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
  adapters/            base (ABC + RetryClientMixin), _subprocess (CLI helper),
                       openrouter, deepseek, claude_api, codex_image, deepgram,
                       elevenlabs, codex_cli, claude_cli
  registry/            models (RegistryModel), service (per-provider catalog sync)
  routing/router.py    ModelRouter.resolve + dispatch
  accounting/cost.py   compute_cost
  storage/database.py  aiosqlite schema + CRUD + logging + usage
  blobs/store.py       on-disk blob store
  mcp_server/          tools (FastMCP), serialize
  web/                 api (JSON, mounted at /api/v1 and /api), views (Run and
                       error envelopes), capabilities (matrix from adapters),
                       labels (the glossary), pages (Jinja2 frames), templates/,
                       static/{app.css, css/tokens.css,
                       js/core/{api,dom,format,state,keys}.js,
                       js/components/{json-view,inspector,timeline}.js,
                       js/pages/{run,models,logs,overview,connections}.js}
tests/                 conftest (one app per session), test_smoke (chain),
                       test_web (app), test_web_views (envelope parity),
                       test_api_filters (log filters/cursor), test_blobs
                       (file-serving headers), test_escaping (no innerHTML),
                       test_capabilities (matrix matches the adapters),
                       test_plan (dry run stays offline), test_registry_patch
                       (partial edit, price survives sync), test_pages_js
                       (page modules against a stub DOM, skipped without node),
                       test_modalities, test_adapters_parse, test_cli,
                       test_fallback, test_registry_sync, test_auth;
                       js/ (DOM stub + API fixtures), mcp_probe.py (manual)
```

Frontend rule: templates are markup only; behaviour lives in native ES modules
under `static/js/`, pages get their module from `page_script`, and static links
carry `?v=<app version>` so a restart cannot leave an old client talking to a
new API.

## Deployment

- Local dev: `uvicorn router_mcp.main:app --host $HOST --port $PORT` (or `python -m router_mcp.main`).
- Production (this host): runs as systemd service `model-router-mcp.service` under a
  dedicated user `modelrouter`, from `/opt/model-router-mcp`, bound to `127.0.0.1:8088`,
  behind nginx + TLS on a subdomain. `Restart=always`; updates via `deploy/deploy.sh`.
  Full runbook: `deploy/DEPLOY.md`. Artifacts: `deploy/model-router-mcp.service`,
  `deploy/install.sh`, `deploy/deploy.sh`, `deploy/nginx-model-router.conf`.
- Secrets/config live in `.env` (see `.env.example`); never committed.
- Runtime data (`data/router.db`, `data/blobs/`) and `logs/` are gitignored.
- Endpoints: `GET /health` (open, status only); `/mcp` (Streamable HTTP, requires
  `Authorization: Bearer $AUTH_TOKEN` when set, enforced by a pure-ASGI middleware);
  web console `/`, `/registry`, `/logs` (guarded by nginx HTTP Basic auth).

## Current state

- ✅ Phases 0–4 + deploy wrapper complete and tested: OpenRouter chat, image
  (Codex), STT (Deepgram), TTS (ElevenLabs), direct DeepSeek + Claude API, and
  CLI engines (`codex`/`claude`).
- ⏳ Phases 5–6 per the plan: registry admin polish, hardening, optional streaming.

See `AGENTS/STATE.md` for the live Now / Next snapshot.

## Data sources & dependencies

- OpenRouter `/models` + `/chat/completions` (requires `OPENROUTER_API_KEY`).
- Reused connection patterns originate in sibling repos: `ort_bot` (OpenRouter,
  Deepgram, ElevenLabs, Codex-image, model cache, DB/config patterns),
  `telecodex_bot` (Codex CLI runner), `openai-auth-sync` (OAuth token sync).
- Codex OAuth shares `auth.json` with those repos — use a dedicated copy to avoid
  refresh-token rotation conflicts.
