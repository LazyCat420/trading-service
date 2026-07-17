#!/bin/bash
# ============================================================
# Trading Backend — Build & Deploy to Synology NAS
#
# Thin wrapper — all logic lives in ../deploy-kit/lib.sh
#
# Usage:
#   npm run deploy              # full deploy
#   npm run deploy -- --dry-run # validate without deploying
#   npm run deploy -- --skip-pull
#   npm run deploy -- --no-cache
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_NAME="trading-service"
DISPLAY_NAME="⚙️ Trading Backend"
SKIP_ENV_DEPLOY=true

PRE_BUILD() {
  local CENTRAL_ENV="${DEPLOY_KIT_DIR}/.env.deploy"
  if [ -f "$CENTRAL_ENV" ]; then
    set -a; source "$CENTRAL_ENV"; set +a
    info "Loaded deploy-kit/.env.deploy"
  fi
}

EXTRA_SSH_SYNC() {
  info "Syncing master .env from vault-service on remote host..."
  ssh "$DEPLOY_SSH_HOST" "cp '${DEPLOY_COMPOSE_ROOT}/vault-service/env/.env' '${DEPLOY_COMPOSE_DIR}/.env'"
  info "Appending concurrency overrides to remote .env..."
  ssh "$DEPLOY_SSH_HOST" "echo '' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'PRISM_URL=http://10.0.0.16:5591/prism-proxy' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'LAZY_TOOL_SERVICE_URL=http://10.0.0.16:5591' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'LAZY_TOOL_SERVICE_PORT=5591' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'V2_TICKER_CONCURRENCY=4' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'ADAPTIVE_MIN_CONCURRENCY=4' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'ADAPTIVE_MAX_CONCURRENCY=8' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'JETSON_MAX_CONCURRENT=6' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'DGX_MAX_CONCURRENT=8' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'ANALYSIS_WORKER_TIMEOUT_SECONDS=1800' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "mkdir -p '${DEPLOY_COMPOSE_DIR}/logs' '${DEPLOY_COMPOSE_ROOT}/notes' 2>/dev/null || sudo mkdir -p '${DEPLOY_COMPOSE_DIR}/logs' '${DEPLOY_COMPOSE_ROOT}/notes'"
  ssh "$DEPLOY_SSH_HOST" "sudo chown -R 1001:1001 '${DEPLOY_COMPOSE_DIR}/logs' '${DEPLOY_COMPOSE_ROOT}/notes'"
  ssh "$DEPLOY_SSH_HOST" "sudo mkdir -p '${DEPLOY_COMPOSE_DIR}/data/charts' && sudo chmod 777 '${DEPLOY_COMPOSE_DIR}/data/charts'"

  # Absorbed scraper-service: ensure cookies.txt exists as a FILE so the compose
  # bind mount (./cookies.txt:/app/cookies.txt) doesn't get created as a directory.
  # If a real (non-empty) local cookies.txt is present, sync it; else leave empty
  # (yt-dlp works without cookies except for age-restricted videos).
  info "Ensuring cookies.txt exists on remote host..."
  ssh "$DEPLOY_SSH_HOST" "touch '${DEPLOY_COMPOSE_DIR}/cookies.txt'"
  if [ -s "${SCRIPT_DIR}/cookies.txt" ]; then
    cat "${SCRIPT_DIR}/cookies.txt" | ssh "$DEPLOY_SSH_HOST" "cat > '${DEPLOY_COMPOSE_DIR}/cookies.txt'"
    ok "cookies.txt synced"
  fi

  info "Syncing lazycat-sdk to remote host..."
  tar --exclude='lazycat-sdk/.venv' --exclude='lazycat-sdk/__pycache__' -czC "${SCRIPT_DIR}/../" lazycat-sdk | ssh "$DEPLOY_SSH_HOST" "sudo mkdir -p '${DEPLOY_COMPOSE_ROOT}/lazycat-sdk' && sudo tar -xzC '${DEPLOY_COMPOSE_ROOT}'"

  info "Syncing .agents folder to remote host..."
  tar -czC "${SCRIPT_DIR}/../" .agents | ssh "$DEPLOY_SSH_HOST" "sudo mkdir -p '${DEPLOY_COMPOSE_DIR}/.agents' && sudo tar -xzC '${DEPLOY_COMPOSE_DIR}'"
  ssh "$DEPLOY_SSH_HOST" "sudo chown -R 1001:1001 '${DEPLOY_COMPOSE_DIR}/.agents'"
}

source "${SCRIPT_DIR}/../deploy-kit/lib.sh"
