# State

Current snapshot. Overwrite each iteration.

## Goal

Build an MCP server (Streamable HTTP) that routes requests to AI models across
providers and modalities, with a model registry, per-external-user request/response
logging, and per-request cost accounting, plus a web test+admin console.

## Now

- ✅ Phase 0 skeleton: config, logging, domain types, aiosqlite storage, blob store.
- ✅ Phase 1 vertical slice: OpenRouter chat adapter (with usage capture) →
  registry (catalog+manual merge) → router → cost → request_logs → MCP tools
  (`list_models`, `chat`, `get_usage`) → FastAPI app (/mcp + /api + pages).
- ✅ Tests green: `pytest` (7 passed) + manual MCP probe over Streamable HTTP.

## Next

- Phase 2: image (Codex OAuth), STT (Deepgram), TTS (ElevenLabs) adapters + tools.
- Phase 3: direct DeepSeek + Claude API adapters.
- Phase 4: CLI engines (`codex` via CodexRunner, `claude`), cwd whitelist.
- Phase 5: registry admin polish, subscription/amortization UI.

## Open questions

- Codex `auth.json` coordination: dedicated copy vs reuse `openai-auth-sync`?
- Real end-to-end chat verification pending an `OPENROUTER_API_KEY` (chain proven
  via stub-adapter tests; no live key used yet).

## Deferred

- Streaming MCP responses (CLI/long chat) — collapsed to final text in v1.
- Full billing reports / monthly amortization rollups.
- Multi-connection concurrency (single shared aiosqlite connection for now).
