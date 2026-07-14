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
- 🔧 Prod claude-cli (2026-07-14): fixed the ProtectHome blocker — the service now
  has a dedicated HOME `/var/lib/model-router/home` (drop-in `claude-cli.conf`,
  tracked in `deploy/`), with an isolated native claude 2.1.187 install inside and
  runs defaulting to `$HOME/workspace` (`CLI_DEFAULT_CWD`, also the only
  `CLI_ALLOWED_CWD_ROOTS` root). Chain verified live: dispatch reaches the CLI,
  which now answers "Not logged in". Pending: one-time subscription login under
  `modelrouter` (interactive OAuth), then end-to-end verify. codex binary for the
  service — still absent. Known quirk: `service_providers.enabled` never
  downgrades on restart (upsert skips it); dispatch re-checks `is_configured()`.
- ✅ Tests green: `pytest` (29 passed).
- ✅ Process: humanizer-ru skill vendored to `.claude/skills/humanizer-ru/` and made
  mandatory for all agent-produced Russian prose (AGENTS.md → Language Rules).

## Next

- Phase 5 (cont.): subscription/amortization UI; tune seed prices.
- Phase 6: hardening, optional streaming for long CLI/chat runs.
- Prod note: the `modelrouter` user needs `codex`/`claude` on its PATH and their
  auth to use CLI engines (codex auth.json is synced; claude needs its own login
  or ANTHROPIC_API_KEY in the service env).

## Open questions

- Codex `auth.json` for the `modelrouter` user (dedicated copy vs reuse keeper's
  via `openai-auth-sync`) — decide when CLI/Codex adapters land.
- Subdomain + TLS cert name for nginx (placeholder `router.example.com`).
- Real end-to-end chat still needs a live `OPENROUTER_API_KEY` (chain proven via
  stub tests).

## Deferred

- Automated update triggers (git-poll timer / inotify `.path` / webhook) — for now
  updates are the manual `deploy.sh`.
- Streaming MCP responses (CLI/long chat); full billing/amortization rollups;
  multi-connection concurrency.
