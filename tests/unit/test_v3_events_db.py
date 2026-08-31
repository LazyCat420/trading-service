"""Pipeline events round-trip through the store the module actually uses.

WHAT THIS FILE USED TO DO, and why it was worse than nothing
------------------------------------------------------------
It called `PipelineStateDB.save_state` / `append_events` / `get_state` — all
three of which have read and written **MongoDB** since the 2026-08-19 cutover —
under the `patch_real_get_db` fixture, which patches
`scripts.migration.pg_connection.get_db`, i.e. **Postgres**. The gate was
therefore attached to a seam the code under test does not touch:

* it skipped unless `TRADING_BOT_TEST_DB` was set, so in practice it never ran
  and its 20 assertions checked nothing;
* and had it run, `save_state`/`append_events` would have written to
  PRODUCTION Mongo, while the cleanup at the end —
  `DELETE FROM pipeline_events ...` through `get_db()` — deleted from the
  frozen Postgres archive. A test that writes one store and tidies another.

The contract worth pinning has nothing to do with which database is underneath:
`append_events` must store what it was given, `get_state(summary_only=False)`
must return it, and `get_state(summary_only=True)` must NOT — the summary is
what the dashboard polls, and a summary that carries every event of a long
cycle is how the status endpoint got slow enough to matter.

So this now drives the real code against an in-memory store. It runs on every
box, with no flag, and it touches no database at all.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.services.pipeline_state import PipelineStateDB


@pytest.fixture
def store(monkeypatch):
    """An in-memory stand-in for mongo_store, with the collections this uses."""
    from app.db import mongo_store

    data: dict[str, list[dict]] = {"pipeline_state": [], "pipeline_events": [],
                                   "analysis_results": []}

    def upsert_doc(collection, key, doc, **kw):
        rows = data.setdefault(collection, [])
        for i, r in enumerate(rows):
            if all(r.get(k) == v for k, v in key.items()):
                rows[i] = {**r, **doc}
                return 1
        rows.append(dict(doc))
        return 1

    def insert_docs(collection, docs, **kw):
        data.setdefault(collection, []).extend(dict(d) for d in docs)
        return len(docs)

    def find_docs(collection, query, sort=None, projection=None, limit=0, **kw):
        rows = [r for r in data.get(collection, [])
                if all(r.get(k) == v for k, v in query.items())]
        return rows[:limit] if limit else rows

    def read_pipeline_events(cycle_id):
        rows = [r for r in data.get("pipeline_events", [])
                if r.get("cycle_id") == cycle_id]
        rows.sort(key=lambda r: str(r.get("timestamp")))
        return [{"ts": r.get("timestamp"), "phase": r.get("phase"),
                 "step": r.get("step"), "detail": r.get("detail"),
                 "status": r.get("status"), "data": r.get("data"),
                 "elapsed_ms": r.get("elapsed_ms")} for r in rows]

    monkeypatch.setattr(mongo_store, "upsert_doc", upsert_doc)
    monkeypatch.setattr(mongo_store, "insert_docs", insert_docs)
    monkeypatch.setattr(mongo_store, "find_docs", find_docs)
    monkeypatch.setattr(mongo_store, "read_pipeline_events", read_pipeline_events)
    monkeypatch.setattr(mongo_store, "backend_for", lambda t: "mongo")
    monkeypatch.setattr(mongo_store, "reads_mongo", lambda t: True)
    monkeypatch.setattr(mongo_store, "writes_pg", lambda t: False)
    return data


EVENTS = [
    {"ts": datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc),
     "phase": "analyzing", "step": "test_step_1", "detail": "Test detail 1",
     "status": "ok", "data": {"some_key": "some_value"}, "elapsed_ms": 123},
    {"ts": datetime(2026, 8, 30, 12, 0, 1, tzinfo=timezone.utc),
     "phase": "trading", "step": "test_step_2", "detail": "Test detail 2",
     "status": "error", "data": {"error_key": "error_value"}, "elapsed_ms": 456},
]


@pytest.fixture
def cycle(store):
    cycle_id = f"test-events-{uuid.uuid4().hex[:6]}"
    PipelineStateDB.save_state({"status": "running", "cycle_id": cycle_id,
                                "tickers": ["AAPL", "MSFT"],
                                "progress": "Testing state", "phase": "running"})
    PipelineStateDB.append_events(cycle_id, EVENTS)
    return cycle_id


def test_the_full_state_carries_every_appended_event(cycle):
    got = PipelineStateDB.get_state(summary_only=False)
    assert got["cycle_id"] == cycle
    assert len(got["events"]) == 2

    ev1 = next(e for e in got["events"] if e["step"] == "test_step_1")
    assert ev1["phase"] == "analyzing"
    assert ev1["detail"] == "Test detail 1"
    assert ev1["status"] == "ok"
    assert ev1["data"]["some_key"] == "some_value"
    assert ev1["elapsed_ms"] == 123

    ev2 = next(e for e in got["events"] if e["step"] == "test_step_2")
    assert ev2["phase"] == "trading"
    assert ev2["status"] == "error"
    assert ev2["data"]["error_key"] == "error_value"
    assert ev2["elapsed_ms"] == 456


def test_the_summary_omits_the_events(cycle):
    """The dashboard polls this. Carrying a long cycle's events makes it slow."""
    got = PipelineStateDB.get_state(summary_only=True)
    assert got["cycle_id"] == cycle
    assert got.get("events") in (None, []), got.get("events")


def test_events_are_stored_against_their_cycle_and_nobody_elses(store, cycle):
    PipelineStateDB.append_events("some-other-cycle", [EVENTS[0]])
    assert len(PipelineStateDB.get_cycle_events(cycle)) == 2
    assert len(PipelineStateDB.get_cycle_events("some-other-cycle")) == 1


def test_an_empty_event_list_writes_nothing(store, cycle):
    before = len(store["pipeline_events"])
    PipelineStateDB.append_events(cycle, [])
    PipelineStateDB.append_events("", EVENTS)
    assert len(store["pipeline_events"]) == before


def test_this_file_touches_no_database(store):
    """The reason the predecessor was dangerous, pinned.

    It wrote through PipelineStateDB (Mongo) and cleaned up through `get_db`
    (Postgres). Nothing here may import either driver.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    bad = sorted(n for n in names
                 if n.startswith("psycopg") or "pg_connection" in n)
    assert bad == [], bad
