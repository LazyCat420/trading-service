#!/bin/bash
# ==============================================================================
# pre_deploy_test.sh — Fast Pre-Deployment Verification
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "🔷 [Pre-Deploy Check] Running verification suite..."

# Activate virtual environment
if [ -d "${ROOT_DIR}/.venv" ]; then
    source "${ROOT_DIR}/.venv/bin/activate"
else
    echo "❌ Error: Virtual environment (.venv) not found in ${ROOT_DIR}" >&2
    exit 1
fi

# The migration interlock, first: it is cheap and it gates a whole class of
# silent damage. If mongo_backends.env and migration_ledger.json disagree, the
# flags shipping to the containers do not match what the migration believes it
# has done -- a table can be reported migrated while Postgres still serves it,
# or cut over with no record that it was.
echo "🧭 Checking the Mongo backend map against the migration ledger..."
if ! python3 "${SCRIPT_DIR}/check_backend_map.py"; then
    echo "❌ [Pre-Deploy Check] Backend map and ledger disagree. Deployment aborted." >&2
    exit 1
fi

# The collection map is hand-authored, so a machine has to keep it honest:
# coverage, injectivity (two tables sharing a collection SILENTLY merges two
# entities in Mongo), naming grammar, and money discipline.
echo "🧭 Checking the Mongo collection map..."
if ! python3 "${SCRIPT_DIR}/check_collection_map.py"; then
    echo "❌ [Pre-Deploy Check] Collection map is invalid. Deployment aborted." >&2
    exit 1
fi

# Run fast unit tests and mocked quality gate tests.
# The migration suites are listed explicitly rather than globbed: this gate is
# meant to stay fast, and a glob would silently pull in every future test file.
echo "🧪 Running unit & mocked regression tests..."
if pytest \
    "${ROOT_DIR}/tests/unit/test_agent_regression.py" \
    "${ROOT_DIR}/tests/unit/test_backend_map_check.py" \
    "${ROOT_DIR}/tests/unit/test_collection_map.py" \
    "${ROOT_DIR}/tests/unit/test_identifier_quoting.py" \
    "${ROOT_DIR}/tests/unit/test_pipeline_state_mongo_read.py" \
    "${ROOT_DIR}/tests/unit/test_mirror_failures_are_visible.py" \
    "${ROOT_DIR}/tests/unit/test_migration_ledger.py" \
    "${ROOT_DIR}/tests/unit/test_mongo_store.py" \
    "${ROOT_DIR}/tests/unit/test_pg_write_guard.py" \
    "${ROOT_DIR}/tests/unit/test_pg_read_guard.py" \
    -q; then
    echo "✅ [Pre-Deploy Check] All tests passed successfully."
else
    echo "❌ [Pre-Deploy Check] Tests failed. Deployment aborted." >&2
    exit 1
fi
