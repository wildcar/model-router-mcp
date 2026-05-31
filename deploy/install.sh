#!/usr/bin/env bash
# First-time install of the systemd service for model-router-mcp.
#
# Prerequisites (manual, see deploy/DEPLOY.md — they need GitHub auth + secrets):
#   - the repo is already cloned to $APP_DIR (via the modelrouter deploy key)
#   - $APP_DIR/.env exists and is filled (chmod 600)
#
# This script (run by a sudo-capable user, e.g. keeper) creates the service user
# if missing, builds the venv, installs deps, templates the unit, and enables it.
#
# Override defaults via env: APP_DIR, SVC_USER, PORT.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/model-router-mcp}"
SVC_USER="${SVC_USER:-modelrouter}"
PORT="${PORT:-8088}"
SERVICE_NAME="model-router-mcp.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ">> install: APP_DIR=${APP_DIR} SVC_USER=${SVC_USER} PORT=${PORT}"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  echo "ERROR: ${APP_DIR} is not a git checkout. Clone it first (see DEPLOY.md)." >&2
  exit 1
fi
if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "ERROR: ${APP_DIR}/.env is missing. Create it from .env.example first." >&2
  exit 1
fi

# 1. Service user (system, no login).
if ! id -u "${SVC_USER}" >/dev/null 2>&1; then
  echo ">> creating system user ${SVC_USER}"
  sudo useradd --system --create-home --shell /usr/sbin/nologin "${SVC_USER}"
fi

# 2. Ownership + runtime dirs.
sudo chown -R "${SVC_USER}:${SVC_USER}" "${APP_DIR}"
sudo -u "${SVC_USER}" mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"
sudo chmod 600 "${APP_DIR}/.env"

# 3. Virtualenv + dependencies (as the service user).
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  echo ">> creating venv"
  sudo -u "${SVC_USER}" python3 -m venv "${APP_DIR}/.venv"
fi
echo ">> installing dependencies"
# Run pip from inside APP_DIR (the editable-install path hook is resolved
# relative to cwd; the service user can't read the invoker's home dir).
sudo -u "${SVC_USER}" bash -c "cd '${APP_DIR}' && .venv/bin/pip install --quiet --upgrade pip && .venv/bin/pip install --quiet -e '.[anthropic]'"

# 4. Template + install the unit.
TMP_UNIT="$(mktemp)"
trap 'rm -f "${TMP_UNIT}"' EXIT
sed -e "s|__APP_DIR__|${APP_DIR}|g" \
    -e "s|__SVC_USER__|${SVC_USER}|g" \
    -e "s|__PORT__|${PORT}|g" \
    "${SCRIPT_DIR}/${SERVICE_NAME}" > "${TMP_UNIT}"
sudo cp "${TMP_UNIT}" "/etc/systemd/system/${SERVICE_NAME}"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}"
sudo systemctl status "${SERVICE_NAME}" --no-pager || true

echo ">> done. Health: curl -fsS http://127.0.0.1:${PORT}/health"
