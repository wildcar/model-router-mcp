#!/usr/bin/env bash
# Manual update + restart for model-router-mcp.
#
# Pulls the latest code (as the service user, via its deploy key), reinstalls
# deps, restarts the systemd service, and waits for /health to come back.
# Run by a sudo-capable user (e.g. keeper):  bash deploy/deploy.sh
#
# Override defaults via env: APP_DIR, SVC_USER, BRANCH, PORT.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/model-router-mcp}"
SVC_USER="${SVC_USER:-modelrouter}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-8088}"
SERVICE_NAME="model-router-mcp.service"
HEALTH_URL="http://127.0.0.1:${PORT}/health"

run_as_svc() { sudo -u "${SVC_USER}" "$@"; }

echo ">> updating ${APP_DIR} (branch ${BRANCH})"
before="$(run_as_svc git -C "${APP_DIR}" rev-parse HEAD)"
run_as_svc git -C "${APP_DIR}" fetch --quiet --prune origin
run_as_svc git -C "${APP_DIR}" checkout --quiet "${BRANCH}"
run_as_svc git -C "${APP_DIR}" reset --quiet --hard "origin/${BRANCH}"
after="$(run_as_svc git -C "${APP_DIR}" rev-parse HEAD)"

if [[ "${before}" == "${after}" ]]; then
  echo ">> already up to date at ${after:0:12} (restarting anyway)"
else
  echo ">> updated ${before:0:12} -> ${after:0:12}"
fi

echo ">> installing dependencies"
run_as_svc "${APP_DIR}/.venv/bin/pip" install --quiet -e "${APP_DIR}[anthropic]"

echo ">> restarting ${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

echo ">> waiting for health"
for i in $(seq 1 30); do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    echo ">> healthy: $(curl -fsS "${HEALTH_URL}")"
    sudo systemctl status "${SERVICE_NAME}" --no-pager | head -5 || true
    exit 0
  fi
  sleep 1
done

echo "ERROR: service did not become healthy within 30s" >&2
sudo journalctl -u "${SERVICE_NAME}" -n 40 --no-pager >&2 || true
exit 1
