# model-router-mcp

An MCP server that routes requests to AI models across providers and modalities
(chat, image, speech-to-text, text-to-speech, CLI). It keeps a registry of
connected models (service-provider → model-provider → model) with cost tiers,
logs every request/response by an external user id, computes per-request cost,
and ships a web console for testing and registry administration.

The MCP surface is served over **Streamable HTTP** at `/mcp`; the same process
serves a web UI at `/`.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,anthropic]"

cp .env.example .env        # then set OPENROUTER_API_KEY (and others as needed)
uvicorn router_mcp.main:app --host 127.0.0.1 --port 8000
```

- Web test console: <http://127.0.0.1:8000/>
- Registry admin: <http://127.0.0.1:8000/registry>
- Request logs & per-user usage: <http://127.0.0.1:8000/logs>
- MCP endpoint (Streamable HTTP): `http://127.0.0.1:8000/mcp`

## MCP tools

| Tool | Purpose |
|------|---------|
| `list_models(modality?, tier?, provider?)` | List registered models |
| `chat(external_user_id, text?/messages?, model_id?, tier?, provider?, params?)` | Route a chat request; returns text + usage + cost |
| `generate_image(external_user_id, prompt, model_id?, provider?, params?)` | Generate an image; returns base64/URLs + cost |
| `transcribe(external_user_id, audio_b64, mime?, model_id?, provider?)` | Speech-to-text |
| `synthesize_speech(external_user_id, text, voice?, model_id?, provider?)` | Text-to-speech; returns base64 audio |
| `run_cli(external_user_id, prompt, engine, cwd?, timeout_sec?)` | Run a local CLI engine (`codex`/`claude`); returns text + usage + cost |
| `get_usage(external_user_id?, since?)` | Aggregated per-user request count, tokens, cost |

The external user id is an explicit tool argument: MCP clients forward their own
end-user id for per-user statistics.

## Deployment

Production runbook (systemd + nginx/TLS, dedicated `modelrouter` user, manual
`deploy.sh`): see [`deploy/DEPLOY.md`](deploy/DEPLOY.md). Quick reference:

```bash
sudo bash deploy/install.sh     # first time (after clone + .env, see runbook)
bash deploy/deploy.sh           # update: pull, reinstall, restart, health-check
```

`GET /health` is open; `/mcp` requires `Authorization: Bearer $AUTH_TOKEN` when
set; the web console sits behind nginx HTTP Basic auth.

## Local models

The `local` provider computes models in the router process itself; the first is
UVDoc (page unwarping, Apache-2.0). Install the CPU runtime and fetch the weights:

```bash
pip install -e ".[local]"          # onnxruntime (CPU), opencv-python-headless, numpy
bash deploy/fetch-models.sh        # sha256-checked download into data/models/
python scripts/uvdoc_smoke.py photo.jpg --out /tmp/uvdoc   # try it on a photograph
```

## Status

Phases 0–4 are complete: OpenRouter chat, image (Codex OAuth), STT (Deepgram),
TTS (ElevenLabs), direct DeepSeek + Claude API, CLI engines (`codex`/`claude`),
registry, per-user logging, cost, web UI, MCP tools, plus a systemd + nginx
deploy wrapper. Remaining: registry admin polish and hardening — see
`AGENTS/SPEC.md` and `AGENTS/STATE.md`.

## Tests

```bash
pytest -q                                   # automated suite (no network)
python tests/mcp_probe.py http://127.0.0.1:8000/mcp   # live MCP probe (server running)
```

## Project docs

Agent/contributor instructions and design live under `AGENTS.md`, `AGENTS/`, and
`docs/adr/`.
