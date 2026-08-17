#!/usr/bin/env bash
# Unit test for the Mongo backend-map precedence in deploy.sh.
#
# It loads the REAL PRE_BUILD / resolve_mongo_backend_map out of deploy.sh
# (with the trailing `source lib.sh` stripped, so no deploy runs) rather than
# reimplementing them — a test that copies the logic cannot see the logic drift.
#
# What it pins, and why it exists: deploy-kit/.env.deploy is gitignored and
# SHARED by every repo's deploy. Sourcing it exported MONGO_STORE_BACKEND into
# the deploy shell, where it beat the in-script default — so any deploy could
# ship a table cutover nobody chose. On 2026-08-16 that file stood at 30 tables
# at `mongo`, money ledger included, while the containers ran 13.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$REPO_DIR"
FAILED=0

pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; FAILED=1; }

# ── load the real functions, minus the final `source .../lib.sh` ─────────────
HARNESS="$(mktemp)"
trap 'rm -f "$HARNESS" "$FAKE_ENV"' EXIT
grep -v '^source "${SCRIPT_DIR}/\.\./deploy-kit/lib\.sh"$' "${SCRIPT_DIR}/deploy.sh" > "$HARNESS"

# Stubs for the deploy-kit logging helpers. `fail` must EXIT, matching
# deploy-kit/lib.sh:136 (`fail() { printf ...; exit 1; }`) — a stub that merely
# returned would let execution continue past a fatal condition and would have
# reported the abort test as broken when the real deploy aborts correctly.
info() { :; }
ok()   { :; }
warn() { WARNINGS="${WARNINGS:-}${1}"$'\n'; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

# a fake .env.deploy standing in for the shared, gitignored file
FAKE_ENV="$(mktemp)"
DEPLOY_KIT_DIR="$(dirname "$FAKE_ENV")"
cat > "$FAKE_ENV" <<'EOF'
MONGO_STORE_BACKEND=positions:mongo,orders:mongo,pipeline_state:mongo
EOF
# PRE_BUILD looks for "$DEPLOY_KIT_DIR/.env.deploy" specifically
mv "$FAKE_ENV" "${DEPLOY_KIT_DIR}/.env.deploy"
FAKE_ENV="${DEPLOY_KIT_DIR}/.env.deploy"

# shellcheck disable=SC1090
source "$HARNESS"
# deploy.sh sets SCRIPT_DIR from $0, which is THIS test — put it back.
SCRIPT_DIR="$REPO_DIR"

CANON="$(grep -E '^MONGO_STORE_BACKEND=' "${REPO_DIR}/app/db/mongo_backends.env" | tail -1 | cut -d= -f2-)"
[ -n "$CANON" ] || { echo "could not read the canonical map — test cannot run" >&2; exit 1; }

echo "test: the shared env file must NOT be able to set the map by itself"
(
  unset MONGO_STORE_BACKEND MONGO_STORE_ALLOW_ENV_OVERRIDE
  PRE_BUILD
  resolve_mongo_backend_map
  [ "$MONGO_STORE_BACKEND" = "$CANON" ] || { echo "MISMATCH|$MONGO_STORE_BACKEND"; exit 1; }
) >/dev/null 2>&1 \
  && pass "leaked .env.deploy value is dropped; committed map wins" \
  || bad "leaked .env.deploy value is dropped; committed map wins" "the committed 13-table map" "the leaked value survived"

echo "test: an explicit human override still works"
(
  unset MONGO_STORE_BACKEND
  export MONGO_STORE_ALLOW_ENV_OVERRIDE=1
  PRE_BUILD
  resolve_mongo_backend_map
  [ "$MONGO_STORE_BACKEND" = "positions:mongo,orders:mongo,pipeline_state:mongo" ] || exit 1
) >/dev/null 2>&1 \
  && pass "MONGO_STORE_ALLOW_ENV_OVERRIDE=1 lets the environment win" \
  || bad "MONGO_STORE_ALLOW_ENV_OVERRIDE=1 lets the environment win" "the env value" "something else"

echo "test: a missing canonical map is fatal, not a silent all-pg deploy"
(
  unset MONGO_STORE_BACKEND MONGO_STORE_ALLOW_ENV_OVERRIDE
  SCRIPT_DIR="$(mktemp -d)"   # no app/db/mongo_backends.env in here
  # `fail` exits, so run the call in its OWN subshell and assert that it died.
  if ( resolve_mongo_backend_map ); then exit 1; else exit 0; fi
) >/dev/null 2>&1 \
  && pass "missing mongo_backends.env aborts the deploy" \
  || bad "missing mongo_backends.env aborts the deploy" "non-zero exit" "resolved anyway"

echo "test: the committed map ships nothing at full mongo except embeddings"
AT_MONGO="$(printf '%s' "$CANON" | tr ',' '\n' | grep ':mongo$' | cut -d: -f1 | sort | tr '\n' ' ')"
if [ "$AT_MONGO" = "embeddings " ]; then
  pass "only embeddings is at full mongo ($AT_MONGO)"
else
  bad "only embeddings is at full mongo" "embeddings " "$AT_MONGO"
fi

echo "test: NEGATIVE CONTROL — the test can actually fail"
(
  unset MONGO_STORE_BACKEND MONGO_STORE_ALLOW_ENV_OVERRIDE
  PRE_BUILD
  resolve_mongo_backend_map
  # deliberately assert the WRONG thing; this comparison must not hold
  [ "$MONGO_STORE_BACKEND" = "positions:mongo,orders:mongo,pipeline_state:mongo" ]
) >/dev/null 2>&1 \
  && bad "negative control" "the leaked value must NOT win" "it won — the guard is not working" \
  || pass "asserting the leaked value wins does fail, so the checks above mean something"

echo
[ "$FAILED" -eq 0 ] && echo "backend map resolution: ALL TESTS PASS" || echo "backend map resolution: FAILURES"
exit "$FAILED"
