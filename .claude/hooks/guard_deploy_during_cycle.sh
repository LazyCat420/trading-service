#!/usr/bin/env bash
# PreToolUse(Bash) — refuse ./deploy.sh while a V3 cycle is running.
#
# Deploying restarts the container and kills the in-flight cycle. That is 30+
# minutes of agent work and, on a --trade run, a half-executed decision set.
# deploy.sh itself has ZERO references to pipeline_state, so nothing stops it.
# I checked by hand before every deploy this session; a hook does not forget.
#
# Exit 2 = block the call and show the reason to the model.
set -uo pipefail
# python3, not jq: jq is NOT installed on this box, and a hook whose parser is
# missing fails SILENTLY — both of these no-opped on every call until a bash -x
# trace showed CMD coming back empty. A hook that cannot report its own absence
# is worse than no hook.
PAYLOAD="$(cat)"
CMD="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)"
case "$CMD" in *deploy.sh*) ;; *) exit 0 ;; esac

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$HOOK_DIR/../.." || exit 0

# An explicit, recorded override for the case where the store is genuinely
# unreachable and a human has confirmed no cycle is running.
if [ "${DEPLOY_SKIP_CYCLE_CHECK:-}" = "1" ]; then
  echo "deploy-gate: live-cycle check skipped by DEPLOY_SKIP_CYCLE_CHECK=1" >&2
  exit 0
fi

# The venv lives in the PRIMARY checkout; a git worktree has none. Checking
# only `./.venv` meant that from any worktree the hook took its "no venv" exit,
# which used to be `exit 0` — so the guard silently disabled itself in exactly
# the trees where the migration work happens.
SUN_ROOT="${CLAUDE_PROJECT_DIR:-/home/lazycat/github/projects/sun}"
for candidate in ".venv/bin/python" "$SUN_ROOT/trading-service/.venv/bin/python"; do
  if [ -x "$candidate" ]; then PY="$candidate"; break; fi
done

if [ -z "${PY:-}" ]; then
  echo "BLOCKED: no python venv found, so the live-cycle check cannot run. An unreadable check is not evidence that no cycle is running. Verify by hand, then re-run with DEPLOY_SKIP_CYCLE_CHECK=1 if it is genuinely idle." >&2
  exit 2
fi

STATUS="$(timeout 25 "$PY" "$HOOK_DIR/_check_cycle_running.py" 2>/dev/null)"

case "$STATUS" in
  running*)
    CID="${STATUS#*|}"
    echo "BLOCKED: a V3 cycle is RUNNING ($CID). Deploying restarts the container and kills it — 30+ min of agent work, and on a --trade run a half-executed decision set. Wait for pipeline_state.status != 'running', or stop the cycle deliberately first." >&2
    exit 2 ;;
  idle*|done*|error*|stopped*|interrupted*)
    exit 0 ;;
  *)
    # FAILS CLOSED, and this is the case that was broken. The probe imported
    # `psycopg2`, which is not installed, so it printed `unknown|` on EVERY
    # call — and `unknown` fell through this case to `exit 0`. The hook has
    # been permitting every deploy while appearing to guard them. An empty
    # STATUS (timeout, missing file) lands here too.
    REASON="${STATUS#*|}"
    echo "BLOCKED: the live-cycle check could not read pipeline_state (${REASON:-no output}). This blocks rather than warns: deploying restarts the container, and an unreadable check is not evidence that no cycle is running. Check with 'cd trading-service && .venv/bin/python scripts/check_pipeline_state.py', then re-run with DEPLOY_SKIP_CYCLE_CHECK=1 if it is genuinely idle." >&2
    exit 2 ;;
esac
