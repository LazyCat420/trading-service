#!/usr/bin/env bash
# PostToolUse(Bash) — flag leaked `idle in transaction` sessions.
#
# Measured 2026-07-29: a one-off psycopg2 script of mine exited without
# commit/close, left a session idle-in-transaction, and that session held a
# lock the migration's ALTER TABLE needed. Two SUBSEQUENT queries hung for
# 120s each and I mistook it for a CORAL bug. The cost is never paid by the
# command that causes it — always by the next one — which is exactly why a
# human (or a model) does not connect the two.
#
# Advisory only: never blocks, only prints. Same trap as
# treesearch-hung-connection.
set -uo pipefail
# python3, not jq: jq is NOT installed on this box, and a hook whose parser is
# missing fails SILENTLY — both of these no-opped on every call until a bash -x
# trace showed CMD coming back empty. A hook that cannot report its own absence
# is worse than no hook.
PAYLOAD="$(cat)"
CMD="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)"
# `*get_db*` was dropped 2026-08-30: no code under app/ has had a get_db
# since the cutover, so that arm only ever matched PROSE and greps — and
# each match opened a Postgres connection to answer a question nobody had.
case "$CMD" in *psycopg2*|*psycopg*|*PG_ARCHIVE_URL*|*DATABASE_URL*) ;; *) exit 0 ;; esac

cd "$(dirname "$0")/../.." || exit 0
[ -x .venv/bin/python ] || exit 0

timeout 20 .venv/bin/python "$(dirname "$0")/_check_idle_txn.py" 2>/dev/null
exit 0
