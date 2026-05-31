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
