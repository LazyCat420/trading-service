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


def test_a_missing_mongo_document_reports_idle_not_a_stale_postgres_row(store):
    """No document means no cycle — and that is a real answer, not a fault.

    REPLACED 2026-08-18. This test used to require that a missing Mongo
    document fall back to Postgres and serve the running cycle it found there,
    which was correct while both stores were written. It is not correct now:
    the module is pure Mongo, and its sibling below
    (`test_at_full_mongo_a_missing_document_does_not_read_postgres`) already
    asserts the OPPOSITE. Two tests in one file contradicting each other means
    one of them is describing a contract that no longer exists.

    The distinction that matters is between ABSENCE and FAILURE. An empty
    collection is a legitimately idle pipeline and must read as `idle`; a read
    that RAISED is unknown and must not (see the read-error test above, and
    IDLE_STATUSES in .claude/hooks/guard_deploy.py:35). Collapsing the two is
    what made a broken read look safe to deploy over.
    """
    store["docs"] = []  # Mongo has nothing yet

    d = PipelineStateDB.get_state(summary_only=True)

    assert d["status"] == "idle"
    assert d["cycle_id"] is None
    assert d.get("error") is None, "an empty collection is not an error state"


def _postgres_is_unreachable(monkeypatch):
    """Make EVERY route to Postgres raise, and return the list of attempts.

    This replaces a patch of `scripts.migration.pg_connection.get_db`.
    `app/services/pipeline_state.py` imports `mongo_query` and `mongo_store`
    and nothing else — it has not named `get_db` since the cutover — so
    patching that function intercepted a call the module cannot make. The test
    passed because there was nothing to catch, which is the same reason it
    would have passed if the module HAD started reading Postgres through any
    other door.

    Sabotaging the import is the version that cannot go quiet: psycopg,
    psycopg2 and the archive pool are all made to explode, so a read attempted
    by any route shows up as a failure of THIS test rather than as a silent
    fallback.
    """
    import builtins

    attempts: list[str] = []
    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if (name.startswith("psycopg")
                or name.startswith("scripts.migration.pg_connection")):
            attempts.append(name)
            raise AssertionError(f"Postgres reached at mode `mongo` via {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    return attempts


def test_the_postgres_tripwire_actually_trips(monkeypatch):
    """NEGATIVE CONTROL for the test below.

    Without this, a tripwire that silently stopped matching would make the
    next test pass forever — which is precisely how its predecessor failed.
    """
    attempts = _postgres_is_unreachable(monkeypatch)
    with pytest.raises(AssertionError, match="Postgres reached"):
        __import__("psycopg")
    assert attempts == ["psycopg"]


def test_at_full_mongo_a_missing_document_does_not_read_postgres(store, monkeypatch):
    """At `mongo` Postgres is frozen, so falling back would serve stale state."""
    store["mode"] = "mongo"
    store["docs"] = []
    attempts = _postgres_is_unreachable(monkeypatch)

    d = PipelineStateDB.get_state(summary_only=True)

    assert attempts == [], f"the module reached for Postgres: {attempts}"
    assert d["status"] == "idle"  # honestly empty, not stale


def test_the_module_under_test_names_no_postgres_at_all():
    """The static half. An import-time tripwire cannot see a lazy import that
    never runs on this code path, so pin the source too."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "app" / "services" / "pipeline_state.py"
    tree = ast.parse(src.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    offenders = sorted(n for n in names
                       if n.startswith("psycopg") or "pg_connection" in n)
    assert offenders == [], f"pipeline_state imports {offenders}"


def test_a_mongo_read_error_is_reported_not_swallowed_into_idle(store, caplog):
    """A read failure must not be reported as an idle pipeline.

    REWRITTEN 2026-08-18, and the change is the point. This used to assert
    that a Mongo outage FELL BACK to Postgres and returned the running cycle
    it found there. That contract is gone: the module is pure Mongo now, there
    is no `connection` import left to patch, and reaching into a store the
    migration is abandoning would be answering with data nobody is writing.

    The invariant the file exists to protect survives intact, because it was
    never really about Postgres — it is that a FAULT MUST NOT LOOK LIKE AN
    IDLE PIPELINE. `status: "idle"` is a member of IDLE_STATUSES in both
    deploy interlocks, so flattening a read error into it tells the guard
    "safe to restart" and the symptom of a broken read is a deploy killing a
    live cycle.

    So the answer on failure is `"unknown"` — which is deliberately NOT in
    IDLE_STATUSES, so the guard refuses the deploy — and the error text is
    carried on the state for whoever reads it.
    """
    store["mode"] = "mongo_read"
    store["raise_on_read"] = True

    with caplog.at_level("ERROR"):
        d = PipelineStateDB.get_state(summary_only=True)

    assert d["status"] == "unknown", (
        "a read failure reported a status the deploy interlock treats as "
        "safe-to-restart"
    )
    assert d["status"] not in {"idle", "done", "error", "stopped", "interrupted"}, (
        "the failure status must stay OUT of IDLE_STATUSES — see "
        ".claude/hooks/guard_deploy.py:35"
    )
    assert "pipeline_state read failed" in (d.get("error") or ""), (
        "the state must carry why it is unknown, not just that it is"
    )
    assert any("Failed to get state" in r.getMessage() for r in caplog.records), (
        f"the failure was not logged; saw: {[r.getMessage() for r in caplog.records]}"
    )
