# State

Current snapshot. Overwrite each iteration.

## Goal

Build an MCP server (Streamable HTTP) that routes requests to AI models across
providers and modalities, with a model registry, per-external-user request/response
logging, and per-request cost accounting, plus a web test+admin console — and run
it as a managed service on this host.

## Now

- ✅ Phases 0–3 + deploy wrapper (deployed and running on the host).
- ✅ Phase 4: CLI engines — `codex` (`codex exec --json`, ported event parsing)
  and `claude` (`claude -p --output-format json`), via the `run_cli` MCP tool and
  `/api/cli`. Shared subprocess helper (`adapters/_subprocess.py`) with exec
  resolution (PATH + nvm/.local fallbacks) and a cwd whitelist
  (`CLI_ALLOWED_CWD_ROOTS`). Claude CLI's `total_cost_usd` is used as a cost
  override. Test console gained a `cli` modality + engine selector.
- ✅ Verified live: `claude` run_cli end-to-end (text + usage + provider cost +
  session_id). Both CLI providers enabled (binaries on PATH).
- ✅ Web UX (Phase 5 start): test console now has a cascading model picker
  (поставщик сервиса → вендор → модель → версия/вариант, derived client-side from
  `model_id`); the chosen provider is sent as a routing hint. Registry price table
  shows USD per 1M tokens (uniform 3-decimal `pricePerMillion`, no exponents) with
  labelled Промпт/Ответ columns; manual-add form takes $/1M and converts to
  per-token on submit.
- ✅ Fixed a startup crash: `resolve_executable` raised `PermissionError` while
  probing `~/.nvm` once that tree appeared under the (ProtectHome-masked) home of
  the `modelrouter` user; now guarded against `OSError`.
- ✅ Deployed to prod and healthy; web console serves the new picker + per-1M prices.
- ✅ Prod claude-cli live (2026-07-14): dedicated service HOME
  `/var/lib/model-router/home` (drop-in tracked as `deploy/claude-cli.conf`),
  isolated native claude 2.1.187 install inside it, one-time subscription login +
  folder trust done under `modelrouter`. Runs execute in `$HOME/workspace`
  (`CLI_DEFAULT_CWD`, also the only `CLI_ALLOWED_CWD_ROOTS` root). Verified
  end-to-end via `/api/cli`: text + usage + session_id; under subscription the CLI
  still reports a notional nonzero `total_cost_usd`, logged with
  `cost_source=provider`.
- ✅ Prod codex-cli live (2026-07-14): codex 0.144.1 (vendored musl binary from the
  npm package) in the service HOME; its own OAuth grant via
  `codex login --device-auth` under `modelrouter` (openai-auth-sync ADR-0002: one
  grant per client, never copy auth.json). Verified end-to-end via `/api/cli`.
- ✅ codex-oauth (image adapter) now shares the service codex CLI's
  `/var/lib/model-router/home/.codex/auth.json` via `CODEX_AUTH_PATH` (dev `.env`
  points at keeper's `~/.codex/auth.json`). One grant, one file: the adapter
  re-reads it per request and re-checks it before refreshing, so both writers stay
  on one rotation chain. The dead Jun-16-family files (`/opt` auth.json + .bak,
  repo-root auth.json) were deleted with user approval on 2026-07-14.
- ✅ Providers now expose live `configured` (adapter key/binary check) next to the
  `enabled` admin toggle: `/api/registry/providers` + «Доступен» column in the
  registry console. `enabled` is seeded from configured state on first insert only
  and never downgraded by restarts; dispatch re-checks `is_configured()` live.
- ✅ Registry freshness pack (2026-07-25, from user feedback after the DeepSeek
  v4-pro incident), four changes in the agreed order:
  1. Truncation is its own error: `ResponseTruncated` when a provider returns no
     content with `finish_reason=length` / `stop_reason=max_tokens`, message names
     the ceiling and the tokens produced. `request_logs.finish_reason` added.
  2. Per-provider catalog sync: `ProviderAdapter.list_catalog()` (OpenRouter with
     prices, DeepSeek and Anthropic ids-only), `RegistryService.sync_all/sync_provider`,
     background timer (`CATALOG_SYNC_INTERVAL_SECONDS`, default 6h) plus
     `POST /api/registry/refresh`. Missing models are deprecated, never deleted;
     seeds are insert-if-absent; the hardcoded `deepseek-chat`/`deepseek-reasoner`
     rows are gone.
  3. Price provenance: `price_source`/`price_source_kind`/`price_checked_at` on
     models, `pricing_url` on providers, unverified prices logged as
     `cost_source='estimated'` and marked in the console.
  3b. Guard found during live verification: a model from a priceless catalog
     stores zeros, which made it look like the cheapest row in the registry.
     `RegistryModel.price_is_unknown` now pushes such models last in automatic
     selection and keeps them off the fallback price ladder.
  4. Fallback policy per request: `{policy: same|cheaper|pricier, max_steps, scope}`
     on chat/image/tts/stt (API + MCP), substitutions reported via
     `requested_model_id` / `fallback_reason` in the response and the log.
- ✅ UI/UX concept for the console, v2 (2026-07-26): `docs/UI_CONCEPT.md` (first
  pass, superseded), three reviews in `docs/reviews/` (API/MCP, frontend
  architecture, unprepared operator), and `docs/UI_CONCEPT_v2.md` merging them.
  Five screens instead of seven, a glossary with visible field captions, a
  six-state money model, output-escaping rules, an in-process run registry with
  polling instead of SSE. Backend asks are split into wave 0..3 with contracts;
  nothing implemented yet.
- ✅ Two P1 defects found by the reviews, both fixed (2026-07-26):
  1. Stored XSS: the log/registry/test pages built rows by interpolating
     `external_user_id`, `request_text`, `model_id` and `fallback_reason` into
     `innerHTML`. `el()`/`row()` helpers in `static/app.js` now set text as text;
     `tests/test_escaping.py` fails the build if `innerHTML` is ever assigned
     anything but an empty literal. Verified by running the real template scripts
     against a stub DOM with `<img onerror>` payloads.
  2. nginx `location /` had no `proxy_read_timeout`, so `POST /api/cli` answered
     504 after 60s while the subprocess kept running (adapter limit 1800s). A
     dedicated `location ~ ^/api/(cli|runs)` gets 3600s with buffering off; the
     rest of the console gets 600s (image generation allows 180s plus a retry).
     Needs `cp` + `nginx -t` + reload on the prod host, see `deploy/DEPLOY.md`.
- ✅ Console wave 0 shipped (2026-07-26), everything in §21 of the concept except
  vendoring htmx/Alpine (deferred to wave 1, when the first fragment needs them):
  1. `/api/v1` is the contract; the bare `/api` prefix serves the same router so
     a page loaded before a restart keeps working. Lists answer
     `{items, total, next_cursor, facets}`.
  2. One `Run` envelope (`web/views.py`) built from a dispatch and from a log
     row, with `tests/test_web_views.py` failing on key drift.
  3. Typed errors: each class carries `code`, `http_status`, `retriable`, `hint`;
     the router attaches `log_id` before the exception leaves, one app handler
     renders the envelope, MCP tools append the same facts to the message.
  4. `request_logs` migration in one pass: channel/tool/client_id/
     client_request_id, attempts, provider_status, provider_request_id,
     session_id, return_code, cache/write/reasoning tokens, characters,
     plan_json, finished_at.
  5. Log reads: column projection, cursor paging by descending id, filters
     (user/status/modality/model/channel/period, with_fallback, with_retries,
     unverified_cost), `GET /logs/{id}` as a `Run`, indexes behind all of it.
  6. `params` reach `/api/tts` and `/api/cli` (TTS language, claude-cli model
     were unreachable from the web API).
  7. Blobs: inline only for image/audio, `nosniff`, immutable cache, byte ranges.
     `/health` trimmed to `{"status": "ok"}`.
  8. Static skeleton: `css/tokens.css`, `js/core/{api,dom,format,state,keys}.js`,
     page logic as ES modules in `js/pages/`, `?v=<version>` on every link.
     `static/app.js` and all inline template scripts are gone.
  9. `ProviderAdapter.SUPPORTED_PARAMS` feeds both the payload and the new
     `ignored_params` on a run; the retry loop records each HTTP try, so the
     two-level timeline has data; a read timeout logs `cost_source='cost_unknown'`.
- ✅ Tests green: `pytest` (73 passed). One app per test session (`tests/conftest.py`):
  FastMCP's session manager runs once per process. `Settings` still reads the repo
  `.env`, so the fixture clears every provider key explicitly.
- ✅ Process: humanizer-ru skill vendored to `.claude/skills/humanizer-ru/` and made
  mandatory for all agent-produced Russian prose (AGENTS.md → Language Rules).

## Next

- Deploy the registry-freshness pack to prod, run `POST /api/registry/refresh`
  there, and fill real prices for the live DeepSeek models in `/registry`
  (they arrive from the catalog with `price_source_kind='unknown'`).
- Copy the updated nginx config to the prod host and reload it (the long-run
  location now also matches `/api/v1/cli`), then re-run a long `/api/cli`
  request to confirm the 504 is gone.
- Deploy wave 0 and watch the first restart: `_add_missing_columns` grows
  sixteen columns on the live `request_logs` in one pass.
- Wave 1 of `docs/UI_CONCEPT_v2.md`: `/api/v1/capabilities` from
  `SUPPORTED_PARAMS` (plus an ADR-0001 addendum), `POST /api/v1/plan` that never
  touches the network, five-item navigation with state in the address, the
  inspector with four tabs and the two-level timeline, the JSON viewer, the
  fallback as one select, `PATCH` on a registry model with the price-protection
  criterion moved to price provenance.
- Phase 5 (cont.): subscription/amortization UI; tune seed prices.
- Phase 6: hardening, optional streaming for long CLI/chat runs.

## Open questions

- Subdomain + TLS cert name for nginx (placeholder `router.example.com`).
- Real end-to-end chat still needs a live `OPENROUTER_API_KEY` (chain proven via
  stub tests).

## Deferred

- Automated update triggers (git-poll timer / inotify `.path` / webhook) — for now
  updates are the manual `deploy.sh`.
- Streaming MCP responses (CLI/long chat); full billing/amortization rollups;
  multi-connection concurrency.
