"""A write to a cut-over table must not reach Postgres quietly.

The migration's worst failure mode is not a crash — it is a `DELETE` that
succeeds against a Postgres table nobody reads any more while the live Mongo
documents stay put. That is what happened to `embeddings` (fixed in 920123c).
These tests pin the backstop: at backend `mongo` the write raises, at `dual`
it does not, and a `SELECT` is never touched.

They exercise the guard directly against a flag map — no database, no pool.
"""

from __future__ import annotations

import importlib
import logging

import pytest


@pytest.fixture
def guard(monkeypatch):
    """The guard module with a controlled backend map."""
    monkeypatch.setenv(
        "MONGO_STORE_BACKEND",
        "embeddings:mongo,analysis_results:mongo_read,cycle_audit_log:dual",
    )
    monkeypatch.delenv("MONGO_GUARD_ALLOW_PG", raising=False)
    from app.db import mongo_store

    importlib.reload(mongo_store)
    from app.db import pg_write_guard

    pg_write_guard.reset_cache()
    yield pg_write_guard
    pg_write_guard.reset_cache()


# ── backend `mongo`: no legitimate PG write exists ─────────────────────────

@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM embeddings",
        "DELETE FROM embeddings WHERE source_table = %s",
        "delete from embeddings where id = 1",
        "INSERT INTO embeddings (source_id) VALUES (%s)",
        "UPDATE embeddings SET vector = %s WHERE id = %s",
        "TRUNCATE embeddings",
        "TRUNCATE TABLE embeddings",
        "DELETE FROM public.embeddings",
        'DELETE FROM "embeddings"',
        "DELETE FROM ONLY embeddings",
        # the write hides inside a CTE — an anchored ^DELETE match misses this
        "WITH gone AS (DELETE FROM embeddings RETURNING id) SELECT count(*) FROM gone",
    ],
)
def test_write_to_cutover_table_raises(guard, sql):
    with pytest.raises(RuntimeError, match="PG GUARD"):
        guard.check_pg_write(sql)


def test_escape_hatch_allows_teardown_tooling(guard, monkeypatch):
    monkeypatch.setenv("MONGO_GUARD_ALLOW_PG", "1")
    guard.check_pg_write("DELETE FROM embeddings")  # must not raise


# ── everything else must be untouched ──────────────────────────────────────

@pytest.mark.parametrize(
    "sql",
    [
        # reads, including of a cut-over table
        "SELECT * FROM embeddings WHERE id = %s",
        "SELECT e.id FROM embeddings e JOIN analysis_results a ON a.id = e.source_id",
        # `dual` is written to both stores on purpose
        "INSERT INTO cycle_audit_log (cycle_id) VALUES (%s)",
        "DELETE FROM cycle_audit_log WHERE cycle_id = %s",
        # an unmigrated table
        "DELETE FROM positions WHERE ticker = %s",
        # DDL: teardown drops these tables deliberately
        "DROP TABLE embeddings",
        "ALTER TABLE embeddings ADD COLUMN x int",
        # a table whose NAME merely contains a guarded name
        "DELETE FROM embeddings_archive WHERE id = %s",
    ],
)
def test_permitted_statements_pass(guard, sql):
    guard.check_pg_write(sql)  # must not raise


def test_app_dual_write_at_mongo_read_is_not_blocked(guard):
    """At `mongo_read` PG is still dual-written — blocking would break the app."""
    guard.check_pg_write("INSERT INTO analysis_results (ticker) VALUES (%s)")
    guard.check_pg_write("UPDATE analysis_results SET x = 1 WHERE id = 2")


# ── `mongo_read` + destructive + script context: warn, don't block ─────────

def test_script_delete_at_mongo_read_logs_error(guard, monkeypatch, caplog):
    monkeypatch.setenv("IS_TOOL_PROCESS", "true")
    with caplog.at_level(logging.ERROR):
        guard.check_pg_write("DELETE FROM analysis_results WHERE ticker = %s")
    assert "PG GUARD" in caplog.text
    assert "analysis_results" in caplog.text


def test_dual_store_delete_suppresses_the_warning(guard, monkeypatch, caplog):
    """A script that deletes from BOTH stores must not be accused of desync."""
    monkeypatch.setenv("IS_TOOL_PROCESS", "true")
    with caplog.at_level(logging.ERROR):
        with guard.dual_store_delete():
            guard.check_pg_write("DELETE FROM analysis_results WHERE ticker = %s")
    assert caplog.text == ""

    # ...and the warning comes back once the marked block exits.
    with caplog.at_level(logging.ERROR):
        guard.check_pg_write("DELETE FROM analysis_results WHERE ticker = %s")
    assert "PG GUARD" in caplog.text


def test_service_delete_at_mongo_read_is_silent(guard, monkeypatch, caplog):
    monkeypatch.delenv("IS_TOOL_PROCESS", raising=False)
    monkeypatch.setattr("sys.argv", ["uvicorn"])
    with caplog.at_level(logging.ERROR):
        guard.check_pg_write("DELETE FROM analysis_results WHERE ticker = %s")
    assert caplog.text == ""


# ── the guard must actually be WIRED into the cursor ───────────────────────
#
# Every test above calls `check_pg_write` directly, so all of them would still
# pass if the call site in `PooledCursor.execute` were deleted. These two go
# through the cursor and assert the statement never reaches psycopg.

class _FakeCursor:
    def __init__(self):
        self.executed = []
        self.description = None

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def executemany(self, sql, seq):
        self.executed.append(sql)

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.autocommit = True
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass


def test_cursor_execute_is_guarded(guard):
    from app.db.connection import PooledCursor

    conn = _FakeConn()
    cur = PooledCursor(conn)
    with pytest.raises(RuntimeError, match="PG GUARD"):
        cur.execute("DELETE FROM embeddings WHERE id = %s", [1])
    assert conn.cur.executed == [], "the write reached psycopg despite the guard"
    cur._closed = True  # suppress the leak warning in __del__


def test_cursor_executemany_is_guarded(guard):
    from app.db.connection import PooledCursor

    conn = _FakeConn()
    cur = PooledCursor(conn)
    with pytest.raises(RuntimeError, match="PG GUARD"):
        cur.executemany("INSERT INTO embeddings (id) VALUES (%s)", [(1,), (2,)])
    assert conn.cur.executed == []
    cur._closed = True


def test_cursor_still_executes_permitted_sql(guard):
    """Negative control: the guard must not break ordinary traffic."""
    from app.db.connection import PooledCursor

    conn = _FakeConn()
    cur = PooledCursor(conn)
    cur.execute("SELECT * FROM embeddings WHERE id = %s", [1])
    cur.execute("INSERT INTO cycle_audit_log (cycle_id) VALUES (%s)", ["c1"])
    assert len(conn.cur.executed) == 2
    cur._closed = True


# ── the guard must be inert before anything is migrated ────────────────────

def test_flag_read_failure_is_not_cached(guard, monkeypatch):
    """A broken flag read must not disable the guard for the whole process.

    The first implementation cached the empty map on any exception, so one
    transient ImportError left every subsequent write unguarded for the life
    of the process — a guard that fails open.
    """
    import app.db

    guard.reset_cache()
    real_module = app.db.mongo_store

    class _Boom:
        def __getattr__(self, name):
            raise ImportError("simulated flag-read failure")

    monkeypatch.setattr(app.db, "mongo_store", _Boom())
    guard.check_pg_write("DELETE FROM embeddings")  # degraded: cannot know

    # The very next call must protect again, with no explicit cache reset.
    monkeypatch.setattr(app.db, "mongo_store", real_module)
    with pytest.raises(RuntimeError, match="PG GUARD"):
        guard.check_pg_write("DELETE FROM embeddings")


def test_no_flags_means_no_guard(monkeypatch):
    monkeypatch.setenv("MONGO_STORE_BACKEND", "")
    from app.db import mongo_store

    importlib.reload(mongo_store)
    from app.db import pg_write_guard

    pg_write_guard.reset_cache()
    try:
        pg_write_guard.check_pg_write("DELETE FROM embeddings")  # must not raise
    finally:
        pg_write_guard.reset_cache()
