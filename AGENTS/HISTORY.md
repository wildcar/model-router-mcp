# History

Newest first. Each entry ≤5 lines using the format defined in `AGENTS.md`.

---

## 2026-06-02 · Fix — startup crash on masked service-user home
- What: resolve_executable now swallows OSError when probing ~/.nvm / candidate paths (PermissionError under systemd ProtectHome=true was crashing startup once /home/modelrouter/.nvm appeared). Added a regression test.
- Why: Prod deploy failed — service exited 3 with PermissionError on /home/modelrouter/.nvm/versions/node.
- Files: router_mcp/adapters/_subprocess.py, tests/test_cli.py
- Next: decide how to actually expose codex/claude to the prod service (PATH/ReadWritePaths vs ProtectHome).

## 2026-06-02 · Web UX — model picker + readable prices
- What: Test console gained a cascading model picker (provider→vendor→model→version, client-side from model_id) and now sends the provider hint; registry price table shows USD/1M tokens (uniform decimals, no exponents) with labelled Промпт/Ответ; manual-add form takes $/1M.
- Why: User: service provider wasn't selectable, OpenRouter list too long, prices shown in exponent form with no unit/label.
- Files: web/templates/test.html, web/templates/registry.html, web/static/app.js, web/static/app.css
- Next: Phase 5 cont. — subscription/amortization UI; tune seed prices.

## 2026-05-31 · Phase 4 — CLI engines (codex, claude)
- What: Added codex/claude CLI adapters (one-shot subprocess + JSON parsing), shared _subprocess helper with cwd whitelist, run_cli MCP tool + /api/cli, cli modality in the test console. Claude CLI total_cost_usd used as cost override.
- Why: Phase 4 — invoke local CLI agents alongside HTTP providers.
- Files: router_mcp/adapters/{_subprocess,codex_cli,claude_cli}.py, bootstrap.py, mcp_server/tools.py, web/{api.py,templates/test.html}, tests/test_cli.py
- Next: Phase 5 — registry admin polish, subscription/amortization UI.

## 2026-05-31 · Phase 3 — direct DeepSeek + Claude API adapters
- What: Added DeepSeek (OpenAI-compatible chat) and Claude (Anthropic Messages, raw httpx) adapters, seeded chat models with approximate prices; verified DeepSeek live end-to-end.
- Why: Phase 3 — direct provider connections beyond OpenRouter.
- Files: router_mcp/adapters/{deepseek,claude_api}.py, bootstrap.py, tests/test_adapters_parse.py
- Next: Phase 4 — CLI engines (codex via CodexRunner, claude).

## 2026-05-31 · Phase 2 — image / STT / TTS modalities
- What: Added Codex-image (OAuth), Deepgram (STT), ElevenLabs (TTS) adapters, seeded into the registry; MCP tools generate_image/transcribe/synthesize_speech; web API /api/image,/api/transcribe,/api/tts; test console modality UI; binary outputs via blob store.
- Why: Phase 2 — broaden modalities beyond chat.
- Files: router_mcp/adapters/{codex_image,deepgram,elevenlabs}.py, bootstrap.py, mcp_server/{tools,serialize}.py, web/{api.py,templates/test.html}, tests/test_modalities.py, tests/test_web.py
- Next: Phase 3 — direct DeepSeek + Claude API adapters.

## 2026-05-31 · Deploy wrapper — systemd + nginx/TLS + deploy.sh
- What: Added `/health`, optional `/mcp` Bearer auth (pure-ASGI, SSE-safe), and deploy artifacts (systemd unit, install.sh, deploy.sh, nginx conf, DEPLOY.md).
- Why: Run model-router-mcp as a managed, auto-restarting, HTTPS-exposed service on this host.
- Files: router_mcp/app.py, router_mcp/config.py, .env.example, deploy/*, docs/adr/0004-deployment-systemd-nginx.md, tests/test_auth.py, tests/test_web.py
- Next: Execute the install on the host; then Phase 2 (image/STT/TTS adapters).

## 2026-05-31 · Phase 0+1 — skeleton & OpenRouter chat slice
- What: Bootstrapped the project and shipped an end-to-end chat slice (OpenRouter → registry → router → cost → request_logs → MCP tools + web UI).
- Why: First iteration of the model-router MCP server per the approved plan.
- Files: router_mcp/* (config, domain, adapters, registry, routing, accounting, storage, blobs, mcp_server, web, app, main, bootstrap), tests/*, pyproject.toml, docs/adr/000{1,2,3}-*.md
- Next: Phase 2 — image (Codex), STT (Deepgram), TTS (ElevenLabs) adapters + tools.
