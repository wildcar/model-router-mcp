# History

Newest first. Each entry ≤5 lines using the format defined in `AGENTS.md`.

---

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
