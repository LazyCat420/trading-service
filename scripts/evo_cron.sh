#!/bin/bash
# ============================================================
# CORAL repair loop — scheduled drain.
#
# `scripts/evo_runner.py` has to run on a host checkout: the trading-service
# image ships source without .git, without the git binary and without pytest,
# so nothing in the container can grade a patch. The container only ENQUEUES.
# Before this script existed nothing drained, and a job sat queued from
# 2026-07-29 until someone noticed on 07-31.
#
# Every tick is a no-op unless there is work, because a real job is expensive:
# the grader runs tests/unit (~3.5 min, 2749 tests) once for the baseline and
# again per candidate, over 2 proposal rounds.
#
# Four guards, in cheapest-first order:
#   1. flock          — never two drains at once
#   2. queue check    — direct SQL, no app import, no migrations; exits in ~1s
#   3. live cycle     — a drain must not steal CPU from the desk
#   4. core cap       — leave cores for everything else even when it does run
#
# Install:  scripts/evo_cron.sh --install
# Remove:   scripts/evo_cron.sh --uninstall
# Dry run:  scripts/evo_cron.sh --check     (guards only, never drains)
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$REPO_ROOT/.venv/bin/python"
LOG_DIR="$REPO_ROOT/logs"
LOG="$LOG_DIR/evo_cron.log"
LOCK="$REPO_ROOT/.evo-worktrees/.cron.lock"
CRON_TAG="# trading-service-coral-drain"
CRON_LINE="20 * * * * $REPO_ROOT/scripts/evo_cron.sh >> $LOG 2>&1 $CRON_TAG"

# The whole drain, including every pytest run. Generous, but bounded: a hung
# proposal must not hold the lock until the next reboot.
DRAIN_TIMEOUT_S=5400

# Leave headroom for the desk and everything else on the box.
CORES="${EVO_CRON_CORES:-6}"
WITH_CORES="$REPO_ROOT/../braindeadbot-client/scripts/with-cores.sh"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

usage_exit() { sed -n '2,25p' "${BASH_SOURCE[0]}"; exit "${1:-0}"; }

# ── install / uninstall ──────────────────────────────────────────────
if [ "${1:-}" = "--install" ]; then
  if crontab -l 2>/dev/null | grep -qF "$CRON_TAG"; then
    echo "already installed:"; crontab -l | grep -F "$CRON_TAG"; exit 0
  fi
  mkdir -p "$LOG_DIR"
  { crontab -l 2>/dev/null; echo "$CRON_LINE"; } | crontab -
  echo "installed:"; crontab -l | grep -F "$CRON_TAG"; exit 0
fi

if [ "${1:-}" = "--uninstall" ]; then
  crontab -l 2>/dev/null | grep -vF "$CRON_TAG" | crontab -
  echo "removed."; exit 0
fi

[ "${1:-}" = "--help" ] && usage_exit 0
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

mkdir -p "$LOG_DIR" "$(dirname "$LOCK")"

# ── guard 1: one drain at a time ─────────────────────────────────────
# A drain can outlast the hourly tick, so this is the difference between one
# slow job and a pile of concurrent pytest runs.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "SKIP: another drain holds the lock"
  exit 0
fi

if [ ! -x "$PY" ]; then
  log "FAIL: no venv interpreter at $PY"
  exit 1
fi

# ── guard 2: is there anything to do? ────────────────────────────────
# Deliberately NOT `evo_runner.py --list`: that boots the app, which runs the
# idempotent auto-migrations. Correct, but not something to do every hour just
# to learn the queue is empty. This reads settings for the URL and nothing else.
queue_state() {
  cd "$REPO_ROOT" && "$PY" - <<'PY' 2>"$ERR_FILE"
import sys
try:
    import psycopg
    from app.config.config import settings
    with psycopg.connect(settings.DATABASE_URL, connect_timeout=10) as c:
        cur = c.cursor()
        cur.execute(
            "SELECT count(*) FROM evolution_repair_queue "
            "WHERE status IN ('queued','running')"
        )
        print(f"QUEUE:{cur.fetchone()[0]}")
        # Liveness is computed here, not parsed from a status string in bash.
        # `status` is not a cycle state machine — the singleton row sat at
        # status='done' with phase='analyzing' (the last phase REACHED). The
        # vocabulary is idle/starting/running/stopping/stopped/done/error/
        # blocked, so an allowlist of "safe" values silently omits `error` and
        # blocks every future drain after one failed cycle. Deny the in-flight
        # values instead, and back it with finished_at, which is unambiguous.
        cur.execute("""
            SELECT status,
                   (status IN ('running','starting','stopping','blocked')
                    OR (started_at IS NOT NULL AND finished_at IS NULL)) AS live
            FROM pipeline_state WHERE singleton_id = 'current'
        """)
        row = cur.fetchone()
        print(f"CYCLE:{row[0] if row else 'none'}")
        print(f"LIVE:{1 if (row and row[1]) else 0}")
except Exception as e:
    print(f"ERROR:{type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
PY
}

# Report WHY it could not check. Skipping is the right response to an
# unreadable queue, but a silent skip is indistinguishable from an empty queue,
# and this script's whole job is to not be the thing that quietly stops running.
ERR_FILE="$(mktemp)"
trap 'rm -f "$ERR_FILE"' EXIT
if ! state="$(queue_state)"; then
  log "SKIP: queue check failed — $(tr '\n' ' ' < "$ERR_FILE" | head -c 300)"
  exit 0
fi
pending="$(sed -n 's/^QUEUE://p' <<<"$state")"
cycle="$(sed -n 's/^CYCLE://p' <<<"$state")"
live="$(sed -n 's/^LIVE://p' <<<"$state")"

if [ "${pending:-0}" -eq 0 ]; then
  [ "$CHECK_ONLY" = 1 ] && log "CHECK: queue empty, cycle=$cycle — would not drain"
  exit 0
fi

# ── guard 3: not while the desk is working ───────────────────────────
# The grader saturates cores for minutes at a time. A repair is never urgent
# enough to slow a live cycle; it waits for the next tick.
if [ "${live:-1}" != "0" ]; then
  log "SKIP: $pending job(s) waiting, but a cycle is live (status=$cycle)"
  exit 0
fi

if [ "$CHECK_ONLY" = 1 ]; then
  log "CHECK: would drain $pending job(s) (cycle=$cycle)"
  exit 0
fi

# ── drain ────────────────────────────────────────────────────────────
log "DRAIN: $pending job(s) queued, cycle=$cycle, cores=$CORES"
cd "$REPO_ROOT" || exit 1

# with-cores.sh is braindeadbot's CPU slot broker; it is not this repo's
# dependency, so fall back to running uncapped rather than not running.
if [ -x "$WITH_CORES" ]; then
  runner=("$WITH_CORES" "CPUS=$CORES" "--wsl" "--")
else
  log "NOTE: CPU broker not found, running uncapped"
  runner=()
fi

timeout "$DRAIN_TIMEOUT_S" "${runner[@]}" "$PY" scripts/evo_runner.py --drain
rc=$?

case $rc in
  0)   log "DRAIN: finished clean" ;;
  124) log "DRAIN: TIMED OUT after ${DRAIN_TIMEOUT_S}s — check for a stuck worktree in .evo-worktrees" ;;
  *)   log "DRAIN: exited $rc" ;;
esac

# Keep the log from growing without bound; cron output is append-only.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  tail -c 2097152 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  log "LOG: truncated to 2MB"
fi

exit $rc
