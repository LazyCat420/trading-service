"""The last-moment live-cycle gate (open item 45).

What these tests pin: the gate FAILS CLOSED, and it reads the store the
pipeline actually writes. Idle is the only state that lets a deploy proceed; a
live cycle blocks it, and so does a database that cannot answer — deploying
into an unknowable state is exactly the 2026-08-11 incident (cycle-v3-1786424970
killed 49s into its run by a deploy whose command-time check had honestly
reported idle).

`test_the_gate_reads_mongo_not_postgres` is the one that would have caught the
2026-08-19 failure: the gate kept reading Postgres after `pipeline_state` moved
to Mongo, so it read a frozen `done` row and waved a deploy through while a
cycle was mid-flight. Every other test here passed throughout that window —
they mocked whichever driver the gate happened to call.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pymongo

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


def _client_returning(doc):
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value.find_one.return_value = doc
    return client


def _run(mod, doc=None, connect_error=None, env=None):
    environ = {"PRISM_MONGO_URI": "mongodb://x/y", "TRADING_MONGO_DB": "trading_bot"}
    environ.update(env or {})
    connect = (
        MagicMock(side_effect=connect_error)
        if connect_error
        else MagicMock(return_value=_client_returning(doc))
    )
    with patch.dict("os.environ", environ, clear=False), \
         patch.object(mod, "ATTEMPTS", 2), \
         patch.object(mod, "RETRY_SLEEP_S", 0), \
         patch("pymongo.MongoClient", connect), \
         patch("dotenv.load_dotenv"):
        return mod.main(), connect


def _state(status, phase="analyzing", cycle_id="cycle-v3-1"):
    return {"singleton_id": "current", "status": status, "phase": phase, "cycle_id": cycle_id}


def test_idle_pipeline_lets_the_deploy_proceed():
    mod = _load()
    code, _ = _run(mod, doc=_state("done"))
    assert code == 0


def test_a_live_cycle_blocks_the_deploy():
    mod = _load()
    code, _ = _run(mod, doc=_state("running", "collecting", "cycle-v3-2"))
    assert code == 1


def test_every_idle_status_is_honoured():
    mod = _load()
    for status in ("idle", "done", "error", "stopped", "interrupted"):
        code, _ = _run(mod, doc=_state(status, "any", "cycle-x"))
        assert code == 0, f"status={status} should be idle"


def test_the_gate_reads_mongo_not_postgres():
    """The store, not just the verdict.

    A gate reading Postgres returns `done` from the frozen archive forever, so
    every other test in this file still passes while the gate is blind. This
    one asserts the query itself: the Mongo client is constructed, the
    `pipeline_state` collection of the trading database is read, and psycopg is
    never touched — importing it would raise here.
    """
    mod = _load()
    with patch.dict(sys.modules, {"psycopg": None}):
        code, connect = _run(mod, doc=_state("running"))
    assert code == 1
    connect.assert_called()
    client = connect.return_value
    client.__getitem__.assert_called_with("trading_bot")
    client.__getitem__.return_value.__getitem__.assert_called_with("pipeline_state")
    coll = client.__getitem__.return_value.__getitem__.return_value
    coll.find_one.assert_called_with({"singleton_id": "current"})


def test_unreachable_database_fails_closed_after_retries():
    mod = _load()
    code, connect = _run(
        mod, connect_error=pymongo.errors.ServerSelectionTimeoutError("no primary"))
    assert code == 1
    assert connect.call_count == 2  # it retried before giving up


def test_a_read_error_after_connecting_also_fails_closed():
    """A client that constructs and then raises on the read is still unknowable.

    pymongo defers server selection to the first operation, so the failure
    surfaces at `find_one`, not at `MongoClient(...)`. A gate that only guarded
    the constructor would fall through with `doc = None` and print "no
    pipeline_state document — treating as idle": an unreachable database
    reading as a quiet desk, which is the exact inversion this gate exists to
    prevent.
    """
    mod = _load()
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value.find_one.side_effect = \
        pymongo.errors.AutoReconnect("connection reset")
    with patch.dict("os.environ", {"PRISM_MONGO_URI": "mongodb://x/y"}, clear=False), \
         patch.object(mod, "ATTEMPTS", 2), \
         patch.object(mod, "RETRY_SLEEP_S", 0), \
         patch("pymongo.MongoClient", MagicMock(return_value=client)), \
         patch("dotenv.load_dotenv"):
        assert mod.main() == 1


def test_missing_mongo_uri_fails_closed():
    mod = _load()
    with patch.dict("os.environ", {"DEPLOY_SKIP_CYCLE_CHECK": ""}, clear=False), \
         patch("dotenv.load_dotenv"):
        import os
        os.environ.pop("PRISM_MONGO_URI", None)
        os.environ.pop("MONGO_URI", None)
        assert mod.main() == 1


def test_no_state_document_reads_as_idle():
    mod = _load()
    code, _ = _run(mod, doc=None)
    assert code == 0


def test_operator_override_skips_the_gate():
    mod = _load()
    # No DB mock at all: with the override set the gate must not even connect.
    with patch.dict("os.environ", {"DEPLOY_SKIP_CYCLE_CHECK": "1"}, clear=False), \
         patch("pymongo.MongoClient", MagicMock(side_effect=AssertionError("must not connect"))):
        assert mod.main() == 0
