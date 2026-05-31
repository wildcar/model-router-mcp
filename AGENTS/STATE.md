# State

Current snapshot. Overwrite each iteration.

## Goal

Build an MCP server (Streamable HTTP) that routes requests to AI models across
providers and modalities, with a model registry, per-external-user request/response
logging, and per-request cost accounting, plus a web test+admin console — and run
it as a managed service on this host.

## Now

- ✅ Phase 0 skeleton + Phase 1 OpenRouter chat slice (registry, routing, cost,
  request_logs, MCP tools `list_models`/`chat`/`get_usage`, web console).
- ✅ Deploy ready: `GET /health`, optional `/mcp` Bearer auth (`AUTH_TOKEN`,
  pure-ASGI middleware — does not buffer SSE), systemd unit + `install.sh` +
  `deploy.sh` + nginx/TLS config + `deploy/DEPLOY.md` runbook.
- ✅ Tests green: `pytest` (11 passed) + live MCP probe (with/without token).

## Next

- Run the install on the host (create `modelrouter`, deploy key, clone, `.env`,
  `install.sh`, nginx + certbot) — manual steps in `deploy/DEPLOY.md`.
- Phase 2: image (Codex OAuth), STT (Deepgram), TTS (ElevenLabs) adapters + tools.
- Phase 3: direct DeepSeek + Claude API adapters.
- Phase 4: CLI engines (`codex`, `claude`).

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
