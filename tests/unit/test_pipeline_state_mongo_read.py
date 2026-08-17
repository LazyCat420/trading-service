"""PipelineStateDB.get_state must survive being pointed at Mongo.

Three defects in the port of this module shared one property: none of them
raised anywhere a test or a human would look. get_state wraps its whole body in
`except Exception -> return default_state()`, and default_state() is
`status: idle`. So a NameError, an AttributeError or a stale read all surface
as a *pipeline that looks idle*.

That is the worst possible failure for this particular table. The deploy
interlock reads pipeline_state to decide whether a cycle is running, and it
treats idle as "safe to restart the container". A silent fault here does not
show up as an error; it shows up as a deploy killing a live cycle.

These tests therefore assert on the *returned state*, not on the absence of an
exception.
"""

from __future__ import annotations

import pytest

from app.services.pipeline_state import PipelineStateDB


RUNNING = {
    "singleton_id": "current",
    "_id": "current",
    "status": "running",
    "cycle_id": "cycle-v3-TEST",
    "phase": "analyzing",
    "tickers": ["NVDA"],
    "progress": "3/8",
    "error": None,
}


@pytest.fixture
def store(monkeypatch):
    """A stand-in mongo_store whose mode is settable per test."""
    from app.db import mongo_store

    state = {"mode": "pg", "docs": [], "raise_on_read": False}

    def backend_for(table):
        return state["mode"] if table == "pipeline_state" else "pg"

    def find_docs(collection, query, sort=None, projection=None, limit=0):
        if state["raise_on_read"]:
            raise RuntimeError("mongo is down")
        return list(state["docs"])

    monkeypatch.setattr(mongo_store, "backend_for", backend_for)
    monkeypatch.setattr(mongo_store, "find_docs", find_docs)
    monkeypatch.setattr(
        mongo_store, "reads_mongo", lambda t: backend_for(t) in ("mongo_read", "mongo")
    )
    monkeypatch.setattr(
        mongo_store, "writes_pg", lambda t: backend_for(t) in ("pg", "dual", "mongo_read")
    )
    return state


def test_a_mongo_read_returns_the_running_state(store, monkeypatch):
    """The plain success path -- and the one find_doc would have broken.

    mongo_store has no `find_doc`; the function is `find_docs`. The AttributeError
    was swallowed and the dashboard would have read `idle` forever.
    """
    store["mode"] = "mongo_read"
    store["docs"] = [dict(RUNNING)]
    d = PipelineStateDB.get_state(summary_only=True)
    assert d["status"] == "running"
    assert d["cycle_id"] == "cycle-v3-TEST"
    assert "_id" not in d and "singleton_id" not in d


def test_a_missing_mongo_document_falls_back_to_postgres_while_pg_is_written(store):
    """At mongo_read the Mongo copy can lag; Postgres is still dual-written.

    The ported code only consulted Postgres when reads_mongo was False, so a
    Mongo singleton that had not caught up reported the pipeline as idle. The
    live Mongo copy was measured 1.4 days stale while a cycle was running.
    """
    store["mode"] = "mongo_read"
    store["docs"] = []  # Mongo has nothing yet
    called = {"pg": False}

    class _Cur:
        description = [("status",), ("cycle_id",), ("singleton_id",)]

        def execute(self, *a, **k):
            called["pg"] = True
            return self

        def fetchone(self):
            return ("running", "cycle-v3-FROM-PG", "current")

    import contextlib

    from app.db import connection

    @contextlib.contextmanager
    def fake_db():
        yield _Cur()

    orig = connection.get_db
    connection.get_db = fake_db
    try:
        d = PipelineStateDB.get_state(summary_only=True)
    finally:
        connection.get_db = orig

    assert called["pg"], "Postgres was never consulted despite being dual-written"
    assert d["status"] == "running"
    assert d["cycle_id"] == "cycle-v3-FROM-PG"


def test_at_full_mongo_a_missing_document_does_not_read_postgres(store):
    """At `mongo` Postgres is frozen, so falling back would serve stale state."""
    store["mode"] = "mongo"
    store["docs"] = []
    called = {"pg": False}

    import contextlib

    from app.db import connection

    @contextlib.contextmanager
    def fake_db():
        called["pg"] = True
        raise AssertionError("Postgres must not be read at mode `mongo`")
        yield  # pragma: no cover

    orig = connection.get_db
    connection.get_db = fake_db
    try:
        d = PipelineStateDB.get_state(summary_only=True)
    finally:
        connection.get_db = orig

    assert not called["pg"]
    assert d["status"] == "idle"  # honestly empty, not stale


def test_a_mongo_read_error_is_reported_not_swallowed_into_idle(store, caplog):
    """A read failure must go through handle_mongo_read_failure, which logs."""
    store["mode"] = "mongo_read"
    store["raise_on_read"] = True

    import contextlib

    from app.db import connection

    class _Cur:
        description = [("status",), ("cycle_id",), ("singleton_id",)]

        def execute(self, *a, **k):
            return self

        def fetchone(self):
            return ("running", "cycle-v3-FROM-PG", "current")

    @contextlib.contextmanager
    def fake_db():
        yield _Cur()

    orig = connection.get_db
    connection.get_db = fake_db
    try:
        with caplog.at_level("WARNING"):
            d = PipelineStateDB.get_state(summary_only=True)
    finally:
        connection.get_db = orig

    assert d["status"] == "running", "a Mongo outage must fall back, not report idle"
    # "PG fallback" is the exact marker the per-wave soak greps container logs
    # for. Asserting on it here keeps the test and the soak looking for the
    # same string, so a reworded log cannot quietly blind the soak.
    assert any("PG fallback" in r.getMessage() for r in caplog.records), (
        "the fallback was not logged with the marker the soak greps for; "
        f"saw: {[r.getMessage() for r in caplog.records]}"
    )
