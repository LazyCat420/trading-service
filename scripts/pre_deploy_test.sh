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

# Run fast unit tests and mocked quality gate tests
echo "🧪 Running unit & mocked regression tests..."
if pytest "${ROOT_DIR}/tests/unit/test_agent_regression.py" -q; then
    echo "✅ [Pre-Deploy Check] All tests passed successfully."
else
    echo "❌ [Pre-Deploy Check] Tests failed. Deployment aborted." >&2
    exit 1
fi
