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

cd "$(dirname "$0")/../.." || exit 0
[ -x .venv/bin/python ] || exit 0

STATUS="$(timeout 25 .venv/bin/python "$(dirname "$0")/_check_cycle_running.py" 2>/dev/null)"

case "$STATUS" in
  running*)
    CID="${STATUS#*|}"
    echo "BLOCKED: a V3 cycle is RUNNING ($CID). Deploying restarts the container and kills it — 30+ min of agent work, and on a --trade run a half-executed decision set. Wait for pipeline_state.status != 'running', or stop the cycle deliberately first." >&2
    exit 2 ;;
esac
exit 0
