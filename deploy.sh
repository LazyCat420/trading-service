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
  ssh "$DEPLOY_SSH_HOST" "echo 'PROVIDER_VLLM_1_URL=http://10.0.0.16:5591/vllm-shim/jetson' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'PROVIDER_VLLM_2_URL=http://10.0.0.16:5591/vllm-shim/gold-spark' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'LAZY_TOOL_SERVICE_URL=http://10.0.0.16:5591' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'LAZY_TOOL_SERVICE_PORT=5591' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'V2_TICKER_CONCURRENCY=4' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'ADAPTIVE_MIN_CONCURRENCY=4' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'ADAPTIVE_MAX_CONCURRENCY=8' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'JETSON_MAX_CONCURRENT=6' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  # 6, not 8: Gold Spark runs exactly 6 requests (measured 2026-08-09 —
  # num_requests_running pinned at 6 while 16+ waited on reason="capacity";
  # the 1M-token max_model_len makes each KV allocation huge). A declared
  # capacity above the real one feeds _total_capacity() a number the box
  # cannot honour.
  ssh "$DEPLOY_SSH_HOST" "echo 'DGX_MAX_CONCURRENT=6' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'ANALYSIS_WORKER_TIMEOUT_SECONDS=1800' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  # Stamp the deployed commit so cycle_main's worker identity reads
  # "<host>/<sha>" instead of "<host>/unknown-build". Any process pointed at
  # the shared database can claim a queued cycle; on 2026-08-05 a local
  # container six weeks behind master took two scheduled cycles and killed
  # both, and nothing in the logs said which instance ran them.
  ssh "$DEPLOY_SSH_HOST" "echo 'GIT_SHA=${GIT_SHA:-$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)}' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'WORKER_NAME=nas-prod' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  # Per-role model benchmarking: which box scores the tournament jury.
  # off|jetson|split — "split" alternates jurors across Gold Spark and Jetson
  # within one tournament so both models score an identical bracket, which is
  # what makes the per-role leaderboard a comparison rather than two separate
  # jobs. Same reason as MONGO_STORE_BACKEND below: the remote .env is
  # overwritten from the vault master above, so this must be appended HERE.
  ssh "$DEPLOY_SSH_HOST" "echo 'TOURNAMENT_JURY_ROUTING=${TOURNAMENT_JURY_ROUTING:-split}' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  # Shadow-benchmark these agents on a second box (answer is recorded, never
  # used). v3_regime_engine is the measured best Jetson candidate: 1.1 loops,
  # so it pays the ~1.9x throughput gap once, off the critical path.
  # v3_portfolio_manager added 2026-08-06. Every box comparison to date comes
  # from v3_regime_engine, whose tools show ZERO calls in 60 days — so all of
  # it is evidence about a tool-LESS job. The gatekeeper is the tool-declaring
  # case (14 tools in its whitelist, and its own prompt tells it to call
  # get_parameters), and until this deploy it was structurally unshadowable:
  # it does not run through agent_runner, which is the only place that
  # dispatched a shadow. Shadowing is off the critical path and cannot reach a
  # decision — see app/v3/model_shadow.py.
  ssh "$DEPLOY_SSH_HOST" "echo 'MODEL_SHADOW_AGENTS=${MODEL_SHADOW_AGENTS:-v3_regime_engine,v3_portfolio_manager}' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  ssh "$DEPLOY_SSH_HOST" "echo 'MODEL_SHADOW_ENDPOINT=${MODEL_SHADOW_ENDPOINT:-jetson}' >> '${DEPLOY_COMPOSE_DIR}/.env'"
  # Postgres→Mongo migration: per-table backend flags (pg|dual|mongo). This
  # service overwrites its .env from the vault master (above), so runtime flags
  # must be appended HERE, not via deploy-kit/.env.deploy (SKIP_ENV_DEPLOY=true).
  # The default below IS the live state — a redeploy without MONGO_STORE_BACKEND
  # exported must not regress any table's backend.
  MONGO_STORE_DEFAULT="pipeline_events:mongo_read,execution_errors:dual,cycle_audit_log:dual,agent_audit_log:dual,llm_audit_logs:mongo_read,agent_traces:dual,agent_tool_telemetry:dual,v3_agent_telemetry:dual,trade_results:mongo_read,ticker_reports:mongo_read,analysis_results:mongo_read,context_blobs:dual,embeddings:mongo"
  ssh "$DEPLOY_SSH_HOST" "echo 'MONGO_STORE_BACKEND=${MONGO_STORE_BACKEND:-$MONGO_STORE_DEFAULT}' >> '${DEPLOY_COMPOSE_DIR}/.env'"
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
