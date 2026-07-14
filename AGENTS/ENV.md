# Environment

Host facts, tools, credentials, and command cheat-sheet for this project.
Update whenever a new tool, credential, or host-specific quirk is learned.

## Host

- **Dev**: Linux, bash, user `keeper`, repo at `/home/keeper/repo/model-router-mcp`.
  `keeper` has passwordless sudo. Python 3.12. Local venv at `.venv`.
- **Prod**: same host. systemd service `model-router-mcp.service` runs as the
  dedicated system user `modelrouter` from `/opt/model-router-mcp`, bound to
  `127.0.0.1:8088`, behind nginx + TLS on a subdomain. Config at
  `/opt/model-router-mcp/.env`; data/logs under `/opt/model-router-mcp/{data,logs}`.

## Tools

- Python 3.11+ (3.12 on host). `pip install -e ".[dev,anthropic]"` in a venv.
- systemd (system + user scope available). nginx + certbot for TLS.
- `gh` CLI is authenticated as GitHub user `wildcar` (repo `wildcar/model-router-mcp`, private).
- `uvicorn` runs the ASGI app; single worker (in-memory core + FastMCP session manager).

## Credentials & secrets

- All secrets live in `.env` (dev: repo root; prod: `/opt/model-router-mcp/.env`,
  chmod 600, owned by `modelrouter`). Referenced by systemd `EnvironmentFile=`.
  Never commit `.env` (gitignored).
- `OPENROUTER_API_KEY` — required for the live catalog and chat.
- `AUTH_TOKEN` — Bearer token required on `/mcp` when set (generate: `openssl rand -hex 32`).
- nginx HTTP Basic for the web console: `/etc/nginx/model-router.htpasswd`.
- Prod git access: a read-only GitHub **deploy key** for user `modelrouter`
  (`/home/modelrouter/.ssh/id_ed25519`) so it can `git pull` autonomously.

## Environments

| Env  | Host | Identifier | Role / account | Where used |
|------|------|------------|----------------|------------|
| dev  | this host | `keeper` | full sudo | `/home/keeper/repo/model-router-mcp`, `.venv` |
| prod | this host | `modelrouter` | system user, nologin | `/opt/model-router-mcp`, systemd, nginx |

## Commands cheat-sheet

### Dev

```bash
. .venv/bin/activate
uvicorn router_mcp.main:app --host 127.0.0.1 --port 8000   # serve
pytest -q                                                  # tests
python tests/mcp_probe.py http://127.0.0.1:8000/mcp        # live MCP probe
```

### Prod

```bash
sudo bash deploy/install.sh            # first-time install (see deploy/DEPLOY.md)
bash deploy/deploy.sh                  # update + restart (pull, deps, restart, health)
sudo systemctl status model-router-mcp
journalctl -u model-router-mcp -f
curl -fsS http://127.0.0.1:8088/health
```

## Host-specific quirks

### Dev

- `pkill -f "uvicorn router_mcp"` matches its own command line and kills the
  invoking shell (exit 144). Use a non-self-matching pattern or `TaskStop`.

### Prod

- FastMCP's session manager can run only once per process → one uvicorn worker.
- Mount detail: `FastMCP(streamable_http_path="/")` + mount at `/mcp` (else `/mcp/mcp`).
- nginx must use `proxy_buffering off` + `proxy_http_version 1.1` on `/mcp` for
  Streamable HTTP / SSE.
- CLI engines (`run_cli`): ProtectHome=true masks all of /home for the service, so
  it has a dedicated HOME=`/var/lib/model-router/home` via the drop-in
  `/etc/systemd/system/model-router-mcp.service.d/claude-cli.conf` (tracked copy:
  `deploy/claude-cli.conf`). claude is natively installed inside that HOME
  (`.local/share/claude/versions/<v>` + `.local/bin/claude` symlink; resolved via
  the `$HOME/.local/bin` fallback in `resolve_executable`). Auth: one-time
  subscription login under `modelrouter` — its own `.credentials.json`; never
  reuse `keeper`'s (two writers race on one refresh token). Runs execute in
  `$HOME/workspace` — `CLI_DEFAULT_CWD`, the only `CLI_ALLOWED_CWD_ROOTS` root,
  and the CLI's trusted folder (empty on purpose: no CLAUDE.md pickup). codex:
  the vendored musl binary from the npm package
  (`@openai/codex-linux-x64/vendor/.../bin/codex`) is copied to the same
  `$HOME/.local/bin`; auth = its own grant via `codex login --device-auth` under
  `modelrouter`. NEVER copy/sync codex `auth.json` between clients — OpenAI
  reuse-detection revokes the whole token family (openai-auth-sync ADR-0002; that
  tool is retired). Both binaries are frozen copies: update manually on new
  versions.
