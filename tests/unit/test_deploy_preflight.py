"""The last-moment live-cycle gate (open item 45).

What these tests pin: the gate FAILS CLOSED. Idle is the only state that lets
a deploy proceed; a live cycle blocks it, and so does a database that cannot
answer — deploying into an unknowable state is exactly the 2026-08-11 incident
(cycle-v3-1786424970 killed 49s into its run by a deploy whose command-time
check had honestly reported idle).
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

_SRC = Path(__file__).resolve().parents[2] / "scripts" / "deploy_preflight.py"


def _load():
    spec = importlib.util.spec_from_file_location("_deploy_preflight_probe", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _conn_returning(row):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.return_value.fetchone.return_value = row
    return conn


def _run(mod, row=None, connect_error=None, env=None):
    environ = {"DATABASE_URL": "postgresql://x/y"}
    environ.update(env or {})
    connect = (
        MagicMock(side_effect=connect_error)
        if connect_error
        else MagicMock(return_value=_conn_returning(row))
    )
    with patch.dict("os.environ", environ, clear=False), \
         patch.object(mod, "ATTEMPTS", 2), \
         patch.object(mod, "RETRY_SLEEP_S", 0), \
         patch("psycopg.connect", connect), \
         patch("dotenv.load_dotenv"):
        return mod.main(), connect


def test_idle_pipeline_lets_the_deploy_proceed():
    mod = _load()
    code, _ = _run(mod, row=("done", "analyzing", "cycle-v3-1"))
    assert code == 0


def test_a_live_cycle_blocks_the_deploy():
    mod = _load()
    code, _ = _run(mod, row=("running", "collecting", "cycle-v3-2"))
    assert code == 1


def test_every_idle_status_is_honoured():
    mod = _load()
    for status in ("idle", "done", "error", "stopped", "interrupted"):
        code, _ = _run(mod, row=(status, "any", "cycle-x"))
        assert code == 0, f"status={status} should be idle"


def test_unreachable_database_fails_closed_after_retries():
    mod = _load()
    code, connect = _run(
        mod, connect_error=psycopg.OperationalError("could not fork"))
    assert code == 1
    assert connect.call_count == 2  # it retried before giving up


def test_missing_database_url_fails_closed():
    mod = _load()
    environ = {"DEPLOY_SKIP_CYCLE_CHECK": ""}
    with patch.dict("os.environ", environ, clear=False), \
         patch("dotenv.load_dotenv"), \
         patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("DATABASE_URL", None)
        assert mod.main() == 1


def test_no_state_row_reads_as_idle():
    mod = _load()
    code, _ = _run(mod, row=None)
    assert code == 0


def test_operator_override_skips_the_gate():
    mod = _load()
    # No DB mock at all: with the override set the gate must not even connect.
    with patch.dict("os.environ", {"DEPLOY_SKIP_CYCLE_CHECK": "1"}, clear=False), \
         patch("psycopg.connect", MagicMock(side_effect=AssertionError("must not connect"))):
        assert mod.main() == 0
