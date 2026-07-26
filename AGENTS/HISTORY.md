# History

Newest first. Each entry ≤5 lines using the format defined in `AGENTS.md`.

---

## 2026-07-26 · Console wave 2: honest money, operations, acceptance in a browser
- What: accounting stopped calling a missing rate a zero — `unpriced`, per-character TTS, cache/reasoning tokens at their own rates with the sum returned by its terms, notional subscription money kept out of real totals, and OpenRouter's own charge captured beside the registry figure; `GET /usage` groups spend five ways and splits the totals by price source with `is_lower_bound`, plus CSV export and the «Расход» tab; `GET /health/summary` and `POST /providers/{id}/probe` behind the gate with «Обзор» rebuilt on them; `GET /mcp/summary` with tool schemas and per-tool/per-client statistics from the log, and the MCP tab; «Модели» gained facet counts returned with the rows, mass deprecation with a listed confirmation and a working undo.
- Why: Wave 2 of `docs/UI_CONCEPT_v2.md`. §7 says the three accounting defects have to be fixed before the spend screen is drawn, or it shows confidently wrong numbers.
- Files: accounting/cost.py, registry/models.py, adapters/{base,openrouter,deepseek,claude_api,elevenlabs,claude_cli}.py, storage/database.py, config.py, bootstrap.py, web/{api,health,mcp_summary,views}.py, web/templates/*, web/static/js/pages/{overview,models,connections,logs,spend}.js, tests/{test_accounting,test_spend,test_health_mcp,test_acceptance}.py, tests/js/*, pyproject.toml
- Fixes-on-the-fly: the browser tests found the «Прогон» form showing no parameters at all when no provider is configured (the matrix was filtered to configured providers with no fallback); the node harness found a duplicate declaration and two helpers lost in a scripted refactor of the models page.
- Next: wave 3 — run registry with progress and cancellation, line-by-line CLI output, `running`/`interrupted` recovery, multi-model mode, retention.

## 2026-07-26 · Console wave 1: capabilities, dry run, five screens
- What: `/api/v1/capabilities` derives the parameter matrix from adapters' new `SUPPORTED_PARAMS` (ADR-0001 addendum) and serves the glossary from `web/labels.py` plus the server-fixed settings by env-var name; `POST /api/v1/plan` explains the routing without touching the network and answers even when nothing can run, with a verdict per rejected candidate; `GET /registry/tree` builds the picker cascade server-side; `PATCH /registry/models/{pair}` edits named columns only, guarded by a revision counter, with price protection re-keyed from row origin to price origin; five-item navigation with state in the address; «Прогон» generated from capabilities with two selection modes, fallback as one select and the estimate on the button; JSON viewer, and one inspector with a two-level timeline serving both «Прогон» and «Журнал».
- Why: Wave 1 of `docs/UI_CONCEPT_v2.md` — the console could show a result but not the path to it, and the form described fields no adapter read.
- Files: router_mcp/web/{capabilities,labels,api,views,pages}.py, web/templates/{base,run,models,logs,overview,connections}.html, web/static/{app.css,js/components/*,js/pages/*,js/core/format.js}, routing/router.py, registry/{service,models}.py, storage/database.py, adapters/*.py, domain/errors.py, docs/adr/{0001,0005}.md, tests/{test_capabilities,test_plan,test_registry_patch,test_pages_js,test_web}.py, tests/js/*
- Fixes-on-the-fly: the optimistic lock used `updated_at`, and two edits inside one second slipped through it — replaced by a `revision` column; sync's price guard was keyed on `source`, so a price typed into a catalog row was wiped by the next sync.
- Next: wave 2 — accounting (cache/reasoning tokens, `unpriced`, `provider_notional`), «Модели» with facets, «Обзор» health summary, the MCP tab, «Расход», Playwright.

## 2026-07-26 · Console wave 0: contracts under the UI
- What: `/api/v1` (bare `/api` kept for in-flight pages); one `Run` envelope built two ways in `web/views.py` with a key-parity test; typed error hierarchy (code + HTTP status + hint + `log_id` attached by the router) behind one handler, MCP tools get the same facts in the message; `request_logs` migration for channel/tool/client, HTTP attempts, provider status and request id, cache/reasoning tokens, `finished_at`; log list projected to table columns with cursor paging and filters, plus `GET /logs/{id}`; `params` reach `/api/tts` and `/api/cli`; blobs answer with a MIME whitelist, nosniff, immutable cache and byte ranges; `/health` trimmed to status; static skeleton (tokens.css, `core/*.js`, page ES modules, `?v=`), `app.js` and inline template scripts gone; `SUPPORTED_PARAMS` per adapter feeds both the payload and `ignored_params`.
- Why: The concept's wave 0 — without these the first UI wave ships on a shape that has to be rewritten by the third screen, and the console keeps hiding the path behind a result (latency, retries, error text and the failing log id all existed and never reached the eye).
- Files: router_mcp/domain/{errors,messages,__init__}.py, adapters/{base,openrouter,deepseek,claude_api,deepgram,elevenlabs,codex_image,codex_cli,claude_cli}.py, routing/router.py, registry/service.py, storage/database.py, mcp_server/tools.py, web/{api,views,pages}.py, web/templates/*, web/static/{app.css,css/tokens.css,js/**}, app.py, tests/{conftest,test_web_views,test_api_filters,test_blobs,test_escaping,test_web,test_modalities}.py, deploy/nginx-model-router.conf, AGENTS/SPEC.md, docs/UI_CONCEPT_v2.md
- Fixes-on-the-fly: the retry loop now records each try, so a 429 with the wait the provider asked for is data instead of a mystery in the latency; a read timeout logs `cost_source='cost_unknown'` rather than a confident zero; one app per test session (FastMCP's session manager runs once per process).
- Next: wave 1 — capabilities from `SUPPORTED_PARAMS`, `POST /plan`, the five-item navigation, the inspector with the two-level timeline.

## 2026-07-26 · Console UI concept v2 plus two P1 fixes it surfaced
- What: `docs/UI_CONCEPT.md` (v1, superseded) plus three reviews in `docs/reviews/` merged into `docs/UI_CONCEPT_v2.md`; fixed the two defects the reviews found in existing code - stored XSS in the console tables (`el()`/`row()` helpers, no `innerHTML` with data, regression test) and the missing nginx read timeout that answered 504 on long CLI runs.
- Why: The console showed the result and hid the path (plan, retries, raw JSON, latency, error text all exist in the data and never reached the screen); the two fixes are prerequisites for any UI work.
- Files: docs/UI_CONCEPT.md, docs/UI_CONCEPT_v2.md, docs/reviews/*, router_mcp/web/static/app.js, router_mcp/web/templates/{logs,registry,test}.html, tests/test_escaping.py, deploy/{nginx-model-router.conf,DEPLOY.md}, AGENTS/STATE.md
- Next: copy the nginx config to prod and reload; decide whether wave 0 of the concept starts now.

## 2026-07-25 · Registry freshness: sync, price provenance, fallback
- What: `ResponseTruncated` + logged `finish_reason`; per-provider `list_catalog()` with TTL/background/manual sync that deprecates (never deletes) retired models and stops seeds resurrecting them; price provenance fields with `cost_source='estimated'` for unconfirmed prices; per-request fallback policy (same/cheaper/pricier) with the substitution reported in the response and the log.
- Why: User feedback after the DeepSeek v4-pro switch: the registry served retired ids, a max_tokens cutoff read as "empty response", and the v4-pro price was copied from deepseek-chat.
- Files: adapters/{base,deepseek,claude_api,openrouter}.py, registry/{models,service}.py, routing/router.py, storage/database.py, accounting/cost.py, bootstrap.py, app.py, config.py, web/{api.py,templates/*}, mcp_server/*, tests/{test_fallback,test_registry_sync,test_web,test_smoke,test_adapters_parse}.py, AGENTS/SPEC.md, .env.example
- Fixes-on-the-fly: models from priceless catalogs stored zeros and looked cheapest — `price_is_unknown` now ranks them last and keeps them off the fallback ladder; `tests/test_web.py` cleared only the OpenRouter key, so the sync was calling the live DeepSeek API during tests.
- Next: deploy, refresh catalogs in prod, fill real DeepSeek prices in /registry.

## 2026-07-14 · Purge dead-family auth.json leftovers
- What: Deleted /opt/model-router-mcp/auth.json (+.bak) and repo-root auth.json — dead Jun 16 token family; nothing references them since the CODEX_AUTH_PATH repoint. Service healthy after removal.
- Why: User confirmed; dead OAuth credentials on disk are pure risk (ADR-0002 reuse-revocation).
- Files: host cleanup only; AGENTS/STATE.md
- Next: Phase 5 cont. — subscription/amortization UI; tune seed prices.

## 2026-07-14 · codex-cli live; codex-oauth shares the CLI grant
- What: `codex login --device-auth` done under modelrouter (verified via /api/cli); CODEX_AUTH_PATH repointed to that same auth.json (dev: keeper's ~/.codex); the image adapter now re-reads the file before refreshing so parallel writers stay on one rotation chain; stale "dedicated copy" docstring advice replaced.
- Why: One OAuth grant per client (openai-auth-sync ADR-0002); the /opt file held a dead Jun 16 token family.
- Files: router_mcp/adapters/codex_image.py, .env.example, AGENTS/{STATE,ENV,HISTORY}.md
- Next: delete dead auth.json leftovers (/opt auth.json + .bak, repo auth.json) after user confirms.

## 2026-07-14 · codex-cli infra + live `configured` flag
- What: codex 0.144.1 (vendored musl binary) installed into the service HOME and verified end-to-end via /api/cli; live `configured` field added to /api/registry/providers + «Доступен» column in the console; bootstrap comment fixed. Temporary auth.json copy removed — per openai-auth-sync ADR-0002 shared token families get revoked, each client logs in itself.
- Why: User asked to bring codex-cli up the same way and fix the misleading stale enabled flag.
- Files: router_mcp/web/api.py, router_mcp/web/templates/registry.html, router_mcp/bootstrap.py, tests/test_web.py, AGENTS/{SPEC,STATE,ENV}.md
- Next: user runs `codex login --device-auth` under modelrouter; re-auth the codex_image /opt token (dead family from Jun 16).

## 2026-07-14 · Prod claude-cli live (login + deploy + verify)
- What: Deployed 295fb28 (CLI_DEFAULT_CWD wiring); user completed the one-time subscription login and folder trust under modelrouter; verified /api/cli end-to-end (pwd → $HOME/workspace, usage, session_id, provider-reported cost).
- Why: Finish the prod claude-cli fix started the same day.
- Files: AGENTS/STATE.md (no code)
- Next: same install recipe for codex-cli; Phase 5 cont. — subscription/amortization UI.

## 2026-07-14 · Prod claude-cli: dedicated HOME + workspace cwd
- What: systemd drop-in (deploy/claude-cli.conf) gives the service HOME=/var/lib/model-router/home (+ReadWritePaths); isolated native claude 2.1.187 install inside it; new CLI_DEFAULT_CWD setting wired into both CLI adapters; prod runs in $HOME/workspace (also the cwd whitelist root).
- Why: ProtectHome=true masked /home — claude-cli was enabled in DB but undispatchable; runs must not execute in /opt (repo CLAUDE.md pickup, read-only fs, secrets nearby).
- Files: router_mcp/config.py, router_mcp/bootstrap.py, .env.example, deploy/claude-cli.conf, AGENTS/ENV.md, AGENTS/SPEC.md, AGENTS/STATE.md
- Next: one-time subscription login under modelrouter; live verify via /api/cli; same install recipe for codex-cli.

## 2026-07-14 · Mandate humanizer-ru skill for Russian prose
- What: Vendored https://github.com/smixs/humanizer-ru (SKILL.md + LICENSE) into .claude/skills/humanizer-ru/, un-ignored .claude/skills/ in .gitignore, added a mandatory Language Rule to AGENTS.md.
- Why: User request — every agent must run its Russian output through the humanizer-ru patterns.
- Files: .claude/skills/humanizer-ru/*, .gitignore, AGENTS.md, AGENTS/STATE.md
- Next: Phase 5 cont.; expose claude/codex binaries + auth to the prod service user (claude-cli currently unusable in prod despite enabled flag).

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
