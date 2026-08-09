# Deployment runbook — model-router-mcp

Target host: this Linux server. The service runs under a dedicated system user
`modelrouter`, from `/opt/model-router-mcp`, behind nginx + TLS on a subdomain.
systemd keeps it alive (`Restart=always`); updates are applied manually with
`deploy/deploy.sh`.

| Thing | Value |
|-------|-------|
| App dir | `/opt/model-router-mcp` |
| Service user | `modelrouter` (system, nologin) |
| Service | `model-router-mcp.service` |
| Bind | `127.0.0.1:8088` (override `PORT`) |
| Env file | `/opt/model-router-mcp/.env` (chmod 600) |
| Data / logs | `/opt/model-router-mcp/{data,logs}` |
| Public URL | `https://router.example.com` (your subdomain) |

Run the steps as `keeper` (has passwordless sudo).

## 1. Create the service user

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin modelrouter
```

## 2. Deploy key (private repo + dedicated user)

So `modelrouter` can `git pull` on its own:

```bash
sudo -u modelrouter ssh-keygen -t ed25519 -N "" -f /home/modelrouter/.ssh/id_ed25519
sudo cat /home/modelrouter/.ssh/id_ed25519.pub
# Add it as a READ-ONLY deploy key (keeper is logged into gh):
gh repo deploy-key add /home/modelrouter/.ssh/id_ed25519.pub \
    -R wildcar/model-router-mcp -t modelrouter-host
# Trust GitHub's host key for the modelrouter user:
sudo -u modelrouter ssh-keyscan github.com | sudo -u modelrouter tee -a /home/modelrouter/.ssh/known_hosts
```

## 3. Clone

```bash
sudo mkdir -p /opt/model-router-mcp
sudo chown modelrouter:modelrouter /opt/model-router-mcp
sudo -u modelrouter git clone git@github.com:wildcar/model-router-mcp.git /opt/model-router-mcp
```

## 4. Configure secrets

```bash
cd /opt/model-router-mcp
sudo -u modelrouter cp .env.example .env
sudo -u modelrouter sed -i \
    -e 's|^HOST=.*|HOST=127.0.0.1|' \
    -e 's|^PORT=.*|PORT=8088|' \
    -e 's|^DB_PATH=.*|DB_PATH=/opt/model-router-mcp/data/router.db|' \
    -e 's|^BLOB_DIR=.*|BLOB_DIR=/opt/model-router-mcp/data/blobs|' \
    -e 's|^LOG_FILE=.*|LOG_FILE=/opt/model-router-mcp/logs/router.log|' \
    .env
# Fill the rest by editing .env (as root/keeper):
#   OPENROUTER_API_KEY=...
#   AUTH_TOKEN=$(openssl rand -hex 32)   # required for /mcp over the internet
sudo chmod 600 .env
```

### Public file links — required whenever Replicate is enabled

Not optional, and the failure is silent until it costs a user a request.
Replicate takes a file input as a URL, or inline as a data URI up to **256 KB**
(`DATA_URI_LIMIT`). A photo taken on a phone is 2–5 MB, so with these unset
*every* image request from a mobile client fails at the adapter:

```bash
#   PUBLIC_BLOB_BASE_URL=https://blob.example.org   # resolves to this service
#   PUBLIC_BLOB_SIGNING_KEY=$(openssl rand -hex 32)
```

Both, not one: without a key no link is ever issued (fail closed), so a base URL
on its own delivers nothing. The host needs its own nginx server block and
certificate pointed at the same upstream — see section 6.

The installation says so itself when they are missing: «Обзор» puts a line in the
work list, the provider reads «настроен наполовину» on «Подключения», and
`POST /api/v1/plan` for a task that carries a file returns it in `warnings`.
Check after the first start:

```bash
curl -fsS -u <web-user> https://router.example.com/api/v1/health/summary | jq .work
```

## 5. Install the service

```bash
cd /opt/model-router-mcp
sudo APP_DIR=/opt/model-router-mcp SVC_USER=modelrouter PORT=8088 bash deploy/install.sh
curl -fsS http://127.0.0.1:8088/health        # -> {"status":"ok",...}
```

`install.sh` creates the venv, installs deps, templates `model-router-mcp.service`
into `/etc/systemd/system/`, and `enable --now`s it.

## 6. nginx + TLS

```bash
sudo cp deploy/nginx-model-router.conf /etc/nginx/sites-available/model-router
# edit server_name -> your subdomain
sudo ln -s /etc/nginx/sites-available/model-router /etc/nginx/sites-enabled/
sudo htpasswd -c /etc/nginx/model-router.htpasswd <web-user>   # web console login
sudo certbot --nginx -d router.example.com
sudo nginx -t && sudo systemctl reload nginx
```

Timeouts matter here: `/mcp` and `/api/cli` get 3600s, the rest of the console
600s. nginx defaults to 60s when `proxy_read_timeout` is unset, which answers 504
on a long `codex`/`claude` run while the subprocess keeps working.

Do **not** copy this file over a config that is already live: certbot rewrites
`/etc/nginx/sites-available/model-router` in place, adding the TLS server block,
the real `server_name` and the 80 to 443 redirect. Copying the repo template over
it drops all three. On a host that already serves the site, port the changed
blocks by hand:

```bash
sudo cp -a /etc/nginx/sites-available/model-router \
           /etc/nginx/sites-available/model-router.bak.$(date +%F-%H%M)
sudo diff -u /etc/nginx/sites-available/model-router \
             /opt/model-router-mcp/deploy/nginx-model-router.conf
sudo nano /etc/nginx/sites-available/model-router     # apply the proxy_* changes
sudo nginx -t && sudo systemctl reload nginx
```

`nginx -t` and the reload cover every site on the host, so read the test output
before reloading. Rollback is `cp` of the `.bak` file plus another reload.

## Updating (routine)

```bash
bash /opt/model-router-mcp/deploy/deploy.sh
```

It pulls `origin/main` (as `modelrouter`), reinstalls deps, restarts the service,
and waits for `/health`. Override `APP_DIR`, `SVC_USER`, `BRANCH`, `PORT` via env.

## Operations

```bash
sudo systemctl status model-router-mcp
sudo systemctl restart model-router-mcp
journalctl -u model-router-mcp -f
curl -fsS http://127.0.0.1:8088/health
```

- **MCP endpoint:** `https://router.example.com/mcp` — clients must send
  `Authorization: Bearer <AUTH_TOKEN>`.
- **Web console:** `https://router.example.com/` — HTTP Basic (htpasswd).
- Crash recovery is automatic (`Restart=always`, `RestartSec=5`); the service is
  `enable`d so it starts on boot.

## Notes / caveats

- `.env`, `data/`, `logs/` are gitignored; `deploy.sh`'s `git reset --hard` leaves
  them untouched.
- **CLI engines (Phase 4):** when `claude`/`codex` adapters land, `modelrouter`
  won't see `keeper`'s OAuth (`~/.codex`, `~/.claude`). Grant the service user a
  readable auth copy (`CODEX_AUTH_PATH`) or run those via `keeper`. The Codex
  refresh token is single-use — use a dedicated `auth.json` (see ADR-0002 notes).
- Port `8088` must be free (`ss -ltnp`); if you change it, update both the unit
  (re-run `install.sh` with `PORT=`) and `nginx-model-router.conf`.
