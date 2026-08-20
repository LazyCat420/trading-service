"""The operator-facing cycle scripts enqueue onto the queue the poller reads.

`cycle_main.poll_system_commands` claims from MongoDB's `v3_system_commands`
and from nothing else. Until 2026-08-19 `scripts/trigger_cycle.py` and
`scripts/observe_cycle.py` INSERTed into the Postgres table of the same name:
the insert committed, both printed success, and no cycle ever started. A
trigger that reports success and starts nothing is the worst shape a tool can
have — so these tests assert the STORE and the claim fields, not just that the
script exits 0.

The claim fields matter as much as the collection: the poller's
`find_one_and_update` filters on `status: "pending"` and sorts by `created_at`.
A document missing either is enqueued into a queue that will never look at it.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    """Import a script module fresh (its `main` is what the operator runs)."""
    src = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_script_{name}", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


@pytest.fixture()
def store():
    """The real `app.db.mongo_store`, with its writers stubbed.

    Patched on the module object rather than swapped into `sys.modules`:
    `observe_cycle` imports `app.db` inside `main()`, so a sys.modules patch
    that ends at import time leaves the live client in place — which the
    conftest production-Mongo guard then (correctly) refuses.
    """
    from app.db import mongo_store as real
    with patch.object(real, "insert_docs", MagicMock()) as ins:
        yield SimpleNamespace(insert_docs=ins)


@pytest.fixture()
def query():
    from app.db import mongo_query as real
    with patch.object(real, "find_rows", MagicMock(return_value=[])) as rows, \
         patch.object(real, "find_row", MagicMock(return_value=None)) as row:
        yield SimpleNamespace(find_rows=rows, find_row=row)


def _enqueued(store):
    assert store.insert_docs.call_count == 1, "exactly one command should be enqueued"
    collection, docs = store.insert_docs.call_args[0][:2]
    assert collection == "v3_system_commands"
    assert len(docs) == 1
    return docs[0]


def test_trigger_cycle_enqueues_onto_mongo_with_the_claim_fields(store, query):
    mod = _load("trigger_cycle")
    with patch.object(sys, "argv", ["trigger_cycle.py", "--tickers", "AAPL,MSFT"]):
        mod.main()
    doc = _enqueued(store)
    assert doc["command_type"] == "START_CYCLE"
    assert doc["status"] == "pending", "the poller only claims status='pending'"
    assert doc["created_at"] is not None, "the poller sorts on created_at"
    assert json.loads(doc["payload"])["tickers"] == ["AAPL", "MSFT"]


def test_trigger_cycle_reads_the_watchlist_from_mongo(store, query):
    query.find_rows.return_value = [("BCE",), ("UBS",)]
    mod = _load("trigger_cycle")
    with patch.object(sys, "argv", ["trigger_cycle.py"]):
        mod.main()
    assert query.find_rows.call_args_list[0][0][0] == "watchlist"
    assert json.loads(_enqueued(store)["payload"])["tickers"] == ["BCE", "UBS"]


def test_trigger_cycle_honours_the_no_trade_flag(store, query):
    mod = _load("trigger_cycle")
    with patch.object(sys, "argv", ["trigger_cycle.py", "-t", "AAPL", "--no-trade"]):
        mod.main()
    assert json.loads(_enqueued(store)["payload"])["trade"] is False


def test_observe_cycle_enqueues_onto_mongo(store, query):
    mod = _load("observe_cycle")
    with patch.object(sys, "argv", ["observe_cycle.py", "--tickers", "JPM", "--no-wait"]):
        assert mod.main() == 0
    doc = _enqueued(store)
    assert doc["command_type"] == "START_V3_CYCLE"
    assert doc["status"] == "pending"
    payload = json.loads(doc["payload"])
    assert payload["tickers"] == ["JPM"]
    assert payload["trade"] is False, "observation runs must not place orders by default"


def test_observe_cycle_refuses_to_queue_behind_a_live_command(store, query):
    query.find_row.return_value = ("cmd-1", "START_CYCLE", "running")
    mod = _load("observe_cycle")
    with patch.object(sys, "argv", ["observe_cycle.py", "--tickers", "JPM"]):
        assert mod.main() == 1
    store.insert_docs.assert_not_called()


def test_neither_script_imports_a_postgres_driver():
    """The regression itself, as source: a Postgres import in either script is
    the defect coming back, and it is invisible to every behavioural test above
    (a script can enqueue to Mongo and still read state from a frozen table)."""
    for name in ("trigger_cycle", "observe_cycle", "check_pipeline_state"):
        src = (SCRIPTS / f"{name}.py").read_text()
        for banned in ("import psycopg", "from psycopg", "pg_connection"):
            assert banned not in src, f"{name}.py still couples to Postgres via `{banned}`"
