# Agent Instructions

Primary entrypoint for any agent (Claude, Codex, DeepSeek, etc.) working in this repository.

## Project

model-router-mcp — an MCP server (Streamable HTTP) that routes requests to AI
models across providers and modalities, with a model registry, per-external-user
request/response logging, cost accounting, and a web test+admin console.

## Environment

- OS / shell: see `AGENTS/ENV.md`
- Commit identity: `wildcar <wildcar@mail.ru>`
- Details, credentials, command cheat-sheet: `AGENTS/ENV.md`

## Document Map

| File | Role |
|------|------|
| `AGENTS.md` | This entrypoint. Workflow, rules, map. |
| `CLAUDE.md` | Compatibility pointer to `AGENTS.md`. |
| `AGENTS/SPEC.md` | Functional specification — source of truth for product behavior. |
| `AGENTS/STATE.md` | Current snapshot: goal, now, next, open questions, deferred. Overwritten each iteration. |
| `AGENTS/HISTORY.md` | Append-only iteration log, newest first. Read only the top few entries. |
| `AGENTS/ENV.md` | Host, tools, credentials, command cheat-sheet. Read on demand. |
| `README.md` | Public-facing readme: what the project is, how to run it locally. |
| `docs/` | Domain / reference docs. Read on demand when a task touches that area. |
| `docs/adr/` | Architecture Decision Records — one file per significant decision (see `docs/adr/TEMPLATE.md`). |

## Startup Checklist

1. Read `AGENTS.md` (this file).
2. Read `AGENTS/SPEC.md`.
3. Read `AGENTS/STATE.md`.
4. Read top 3–5 entries in `AGENTS/HISTORY.md`.
5. Check `git status --short` before editing; do not overwrite unrelated user changes.

Open `AGENTS/ENV.md` only when you need environment details. Open the relevant file under `docs/` when the task touches that domain.

## Change Workflow

For every iteration that changes code or behavior:

1. If the functional contract changes — update `AGENTS/SPEC.md` first.
2. Make the changes.
3. Overwrite `AGENTS/STATE.md` to reflect the new current state.
4. Prepend a new entry to `AGENTS/HISTORY.md` using the format below.
5. Commit and push after successful verification.

### `AGENTS/HISTORY.md` entry format (≤5 lines, newest first)

```
## YYYY-MM-DD · <short iteration title>
- What: <one line — what changed>
- Why: <one line — reason / task>
- Files: <key paths, comma-separated>
- Next: <one line — what was planned right after>
```

Keep each entry tight. Long explanations belong in commit messages or `SPEC.md`. An optional `Fixes-on-the-fly:` line is fine when an iteration also corrected small things discovered mid-way.

When you ship a deferred item from `STATE.md`, write a normal HISTORY entry and remove the item from `STATE.md`.

## Language Rules

- Source code, technical docs, code comments: English.
- Conversation with the user: Russian.
- End-user UI text: Russian, with ability to extend to other languages.
- Existing docs already written in another language are an established contract — keep editing them in that language; don't silently translate.

## Project Rules

Hard constraints and invariants this project must not violate. Fill in after the first iteration; keep each rule one line.

<!-- Examples of the kind of rule that belongs here:
- Do not delete files — move retired code to `deprecated/` (or `git mv`) so git history survives.
- Library / framework lock-ins ("X is the only allowed library for Y").
- Runtime / boundary invariants the agent must respect.
-->

- `domain/` holds pure types only — no imports from adapters/storage/mcp/web (keep the graph acyclic).
- Never store binary payloads in SQLite — use `blobs/store.py`; the DB keeps refs.
- `external_user_id` is unauthenticated free-form input — a stats/grouping key, never a trust boundary.
- One FastMCP instance per process; its session manager runs once (see ADR-0003).
- Reuse sibling-repo connection patterns; don't add a second uncoordinated writer to Codex `auth.json`.

## Stack & Commands

Python 3.11+ · FastMCP (Streamable HTTP) · FastAPI + uvicorn · httpx · pydantic-settings · aiosqlite. Full env detail in `AGENTS/ENV.md`.

```bash
# install      python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,anthropic]"
# dev / run    uvicorn router_mcp.main:app --host 127.0.0.1 --port 8000   (config via .env)
# test         pytest -q
# mcp probe    python tests/mcp_probe.py http://127.0.0.1:8000/mcp   (server must be running)
# lint         (none configured yet)
```

## Architecture

```
router_mcp/
  app.py / main.py / bootstrap.py / runtime.py   server wiring + core handle
  config.py / logging_config.py                  settings, logging
  domain/      pure types: modality, messages (RouterRequest/Response/Usage), errors
  adapters/    ProviderAdapter ABC + RetryClientMixin + _subprocess (CLI helper);
               openrouter, deepseek, claude_api, codex_image, deepgram, elevenlabs,
               codex_cli, claude_cli
  registry/    RegistryModel + RegistryService (OpenRouter catalog + seed/manual merge)
  routing/     ModelRouter.resolve + dispatch (cost + logging)
  accounting/  compute_cost (per-request / amortized hook)
  storage/     aiosqlite Database (service_providers, registry_models, subscriptions, request_logs)
  blobs/       on-disk blob store (binary payloads)
  mcp_server/  FastMCP tools (list_models, chat, generate_image, transcribe,
               synthesize_speech, run_cli, get_usage) + serialize
  web/         JSON API (/api/*), Jinja2 pages (/, /registry, /logs), static, /blobs
```

## Code Style

- Python, `from __future__ import annotations`; type hints throughout; dataclasses for records.
- Absolute imports rooted at `router_mcp.`; the MCP subpackage is `mcp_server` to avoid shadowing the `mcp` SDK.
- Errors normalize to `domain/errors.py` (RouterError / NotConfigured / ProviderUnavailable / AccessDenied / ProviderError).
- Match the conventions of surrounding code: comment density, naming, idiom.
