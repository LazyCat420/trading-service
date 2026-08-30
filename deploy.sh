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

  # .env.deploy is gitignored and SHARED by every repo's deploy, and the source
  # above exports whatever MONGO_STORE_BACKEND it holds into this shell — where
  # it used to beat the in-script default below. That made any deploy, for any
  # reason, able to ship a table cutover nobody chose: on 2026-08-16 the file
  # stood at 30 tables at `mongo` (money ledger + pipeline_state included) while
  # the containers ran 13. At `mongo` a Postgres write RAISES, so shipping it
  # ahead of the code breaks every write to those tables.
  # The canonical map is app/db/mongo_backends.env, which is COMMITTED.
  if [ -n "${MONGO_STORE_BACKEND:-}" ]; then
    if [ "${MONGO_STORE_ALLOW_ENV_OVERRIDE:-}" = "1" ]; then
      warn "MONGO_STORE_BACKEND taken from the environment (explicit override)"
    else
      warn "ignoring MONGO_STORE_BACKEND from the environment — app/db/mongo_backends.env wins"
      warn "  (set MONGO_STORE_ALLOW_ENV_OVERRIDE=1 for this run to use the environment instead)"
      unset MONGO_STORE_BACKEND
    fi
  fi

  # Same file, same problem, different variable: .env.deploy line 26 holds
  # treesearch-service's asyncpg DSN for the SHARED trading_bot database,
  # and sourcing it above put a live Postgres DSN into this deploy shell —
  # and into anything run from it afterwards. Nothing in this image reads
  # one (no driver since 2026-08-18), and a DSN lying around in the ambient
  # environment is precisely how legacy scripts kept answering from the
  # frozen archive. Dropped for the rest of this shell.
  if [ -n "${DATABASE_URL:-}" ]; then
    warn "dropping DATABASE_URL inherited from deploy-kit/.env.deploy — this image has no Postgres driver"
    unset DATABASE_URL
  fi
}

# Resolve the per-table Mongo backend map into MONGO_STORE_BACKEND.
#
# Precedence, deliberately: an explicitly opted-in environment value, else the
# COMMITTED app/db/mongo_backends.env. Never a value that merely leaked in from
# sourcing the shared, gitignored deploy-kit/.env.deploy — PRE_BUILD has already
# dropped that. Kept as its own function so the precedence is unit-testable
# without running a deploy (scripts/test_backend_map_resolution.sh).
resolve_mongo_backend_map() {
  local BACKEND_FILE="${SCRIPT_DIR}/app/db/mongo_backends.env"
  if [ -n "${MONGO_STORE_BACKEND:-}" ]; then
    info "Mongo backend map: from the environment (explicit override)"
  else
    [ -f "$BACKEND_FILE" ] || fail "app/db/mongo_backends.env is missing — it is the canonical backend map; refusing to deploy an unknown flag state"
    MONGO_STORE_BACKEND="$(grep -E '^MONGO_STORE_BACKEND=' "$BACKEND_FILE" | tail -1 | cut -d= -f2-)"
    [ -n "$MONGO_STORE_BACKEND" ] || fail "app/db/mongo_backends.env has no MONGO_STORE_BACKEND= line"
  fi
  local n_tables n_mongo
  n_tables=$(printf '%s' "$MONGO_STORE_BACKEND" | tr ',' '\n' | grep -c ':')
  n_mongo=$(printf '%s' "$MONGO_STORE_BACKEND" | tr ',' '\n' | grep -c ':mongo$')
  info "Mongo backend map: ${n_tables} tables, ${n_mongo} at full mongo"
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
  # Postgres→Mongo migration: per-table backend flags (pg|dual|mongo_read|mongo).
  # This service overwrites its .env from the vault master (above), so runtime
  # flags must be appended HERE, not via deploy-kit/.env.deploy
  # (SKIP_ENV_DEPLOY=true).
  #
  # The map is read from the COMMITTED app/db/mongo_backends.env. It is not
  # duplicated inline any more: an inline default that a sourced env file could
  # silently beat is how a 30-table cutover came to be armed while the comment
  # above it claimed to be the live state (ch.66 F1). PRE_BUILD has already
  # dropped any environment value unless a human opted in for this run.
  resolve_mongo_backend_map
  ssh "$DEPLOY_SSH_HOST" "echo 'MONGO_STORE_BACKEND=${MONGO_STORE_BACKEND}' >> '${DEPLOY_COMPOSE_DIR}/.env'"
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

  # Open item 45: the command-time cycle check proves the desk was idle when
  # the deploy STARTED; the ~150s build lets the scheduler start a cycle in
  # that window (killed cycle-v3-1786424970 on 2026-08-11). This hook is the
  # last user-owned seam before lib.sh transfers the image and swaps the
  # container, so re-check HERE. Non-zero exit aborts the deploy (set -e).
  info "Live-cycle gate (deploy_preflight) — last check before the swap..."
  local _preflight_py="${SCRIPT_DIR}/.venv/bin/python"
  [ -x "$_preflight_py" ] || _preflight_py="python3"
  if ! "$_preflight_py" "${SCRIPT_DIR}/scripts/deploy_preflight.py"; then
    echo "ABORTING DEPLOY — see deploy_preflight output above." >&2
    return 1
  fi
}

source "${SCRIPT_DIR}/../deploy-kit/lib.sh"
