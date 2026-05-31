# State

Current snapshot. Overwrite each iteration.

## Goal

Build an MCP server (Streamable HTTP) that routes requests to AI models across
providers and modalities, with a model registry, per-external-user request/response
logging, and per-request cost accounting, plus a web test+admin console — and run
it as a managed service on this host.

## Now

- ✅ Phases 0–2 + deploy wrapper (deployed and running on the host).
- ✅ Phase 3: direct DeepSeek adapter (OpenAI-compatible chat) and Claude API
  adapter (Anthropic Messages, raw httpx). Seeded chat models with approximate
  prices (editable in /registry). Routed by the existing `chat` tool / `/api/chat`.
- ✅ Verified live: DeepSeek chat end-to-end (text + real tokens + computed cost +
  log + usage). `anthropic-api` disabled until `ANTHROPIC_API_KEY` is set.
- ✅ Tests green: `pytest` (20 passed).

## Next

- Phase 4: CLI engines — `codex` (reuse telecodex CodexRunner) and `claude`
  (`claude -p ... --output-format json`), `run_cli` tool, cwd whitelist.
- Phase 5: registry admin polish; subscription/amortization UI.
- Set Anthropic key on the host if Claude API is wanted; tune seed model prices.

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
