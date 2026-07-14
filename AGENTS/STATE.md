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
  on one rotation chain. Dead Jun-16-family files remain on disk pending deletion
  approval: `/opt/model-router-mcp/auth.json{,.bak}`, repo-root `auth.json`.
- ✅ Providers now expose live `configured` (adapter key/binary check) next to the
  `enabled` admin toggle: `/api/registry/providers` + «Доступен» column in the
  registry console. `enabled` is seeded from configured state on first insert only
  and never downgraded by restarts; dispatch re-checks `is_configured()` live.
- ✅ Tests green: `pytest` (29 passed).
- ✅ Process: humanizer-ru skill vendored to `.claude/skills/humanizer-ru/` and made
  mandatory for all agent-produced Russian prose (AGENTS.md → Language Rules).

## Next

- Delete the dead-family auth.json leftovers once the user confirms:
  `/opt/model-router-mcp/auth.json`, its `.bak`, and repo-root `auth.json`.
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
