#!/bin/bash
# ============================================================
# Trading Cycle Backend — Entrypoint
# ============================================================
# Runs the autonomous trading cycle scheduler.
# Override behavior with environment variables:
#   CYCLE_ONCE=true     — run one cycle and exit
#   CYCLE_TICKERS=AAPL  — override ticker list
# Scheduling is driven by the v3_system_commands poller — there is no
# interval flag; enqueue START_CYCLE commands to trigger cycles.
# ============================================================

set -e

echo "[cycle-backend] Python: $(/opt/venv/bin/python --version 2>&1 || echo 'NOT FOUND')"
echo "[cycle-backend] Starting trading cycle backend..."

# Ensure log directories exist (volume mounts override Dockerfile mkdir)
mkdir -p logs/cycles logs/v2 memory 2>/dev/null || true

ARGS=""

if [ "${CYCLE_ONCE}" = "true" ]; then
    ARGS="$ARGS --once"
fi

if [ -n "${CYCLE_TICKERS}" ]; then
    ARGS="$ARGS --tickers ${CYCLE_TICKERS}"
fi

exec /opt/venv/bin/python cycle_main.py $ARGS
