"""The read guard: a SELECT against a frozen table must not look like success.

After a table reaches mode `mongo` its Postgres rows stop changing, so a
surviving reader does not fail -- it returns stale data that looks current.
`embeddings` is the live proof: at `mongo` since 2026-07-25, with 701 vectors
written since then that exist only in Mongo. A reader still pointed at Postgres
would serve a 2.4%-short index, silently, forever.

The guard is opt-in (MONGO_GUARD_BLOCK_READS=1) because it is a soak
instrument. These tests therefore check three states, not two: armed-and-fires,
armed-and-correctly-silent, and disarmed.
"""

from __future__ import annotations

import pytest

from app.db import pg_write_guard as guard


@pytest.fixture
def modes(monkeypatch):
    """Set the guarded-table map directly; no env parsing, no import order."""
    def _set(mapping: dict[str, str], *, block_reads=True, allow_override=False):
        monkeypatch.setattr(guard, "_guarded_cache", dict(mapping))
        monkeypatch.setenv("MONGO_GUARD_BLOCK_READS", "1" if block_reads else "0")
        monkeypatch.setenv("MONGO_GUARD_ALLOW_PG", "1" if allow_override else "")
    yield _set
    guard.reset_cache()


# ── it fires ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "SELECT * FROM embeddings",
    "SELECT id FROM public.embeddings WHERE ticker = %s",
    'SELECT id FROM "embeddings"',
    "SELECT e.id FROM analysis_results a JOIN embeddings e ON e.id = a.id",
    "SELECT * FROM (SELECT id FROM embeddings) x",          # subquery
    "WITH c AS (SELECT id FROM embeddings) SELECT * FROM c",  # CTE body
])
def test_a_read_of_a_frozen_table_raises(modes, sql):
    modes({"embeddings": "mongo"})
    with pytest.raises(RuntimeError, match=r"\[PG GUARD\].*embeddings"):
        guard.check_pg_read(sql)


def test_the_write_path_also_applies_the_read_guard(modes):
    """check_pg_write is the hook on every execute; reads must route through it."""
    modes({"embeddings": "mongo"})
    with pytest.raises(RuntimeError, match=r"\[PG GUARD\]"):
        guard.check_pg_write("SELECT * FROM embeddings")


# ── it stays silent when it should ─────────────────────────────────────────

def test_a_mongo_read_table_is_not_guarded(modes):
    """At mongo_read Postgres is still dual-written -- reading it is legitimate."""
    modes({"pipeline_events": "mongo_read"})
    guard.check_pg_read("SELECT * FROM pipeline_events")


def test_an_unmigrated_table_is_not_guarded(modes):
    modes({"embeddings": "mongo"})
    guard.check_pg_read("SELECT * FROM watchlist")


def test_disarmed_by_default(modes):
    """Without the env var the guard must not fire, or every wave breaks at once."""
    modes({"embeddings": "mongo"}, block_reads=False)
    guard.check_pg_read("SELECT * FROM embeddings")


def test_the_override_downgrades_to_a_warning(modes, caplog):
    modes({"embeddings": "mongo"}, allow_override=True)
    with caplog.at_level("WARNING"):
        guard.check_pg_read("SELECT * FROM embeddings")
    assert any("PG GUARD" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("sql", [
    "SELECT EXTRACT(EPOCH FROM MAX(timestamp) - MIN(timestamp)) FROM watchlist",
    "SELECT SUBSTRING(name FROM 1 FOR 3) FROM watchlist",
    "SELECT * FROM generate_series(1, 10)",
    "SELECT * FROM unnest(ARRAY[1,2])",
])
def test_sql_that_merely_contains_from_is_not_a_table_read(modes, sql):
    """`EXTRACT(EPOCH FROM ...)` is not a read of a table called `epoch`."""
    modes({"embeddings": "mongo", "epoch": "mongo", "watchlist": "mongo_read"})
    guard.check_pg_read(sql)


# ── the guard must not fail open ───────────────────────────────────────────

def test_an_empty_statement_is_not_an_error(modes):
    modes({"embeddings": "mongo"})
    guard.check_pg_read("")


def test_the_write_guard_still_works(modes):
    """Regression: adding the read path must not disturb the write path."""
    modes({"embeddings": "mongo"})
    with pytest.raises(RuntimeError, match=r"refusing INSERT INTO"):
        guard.check_pg_write("INSERT INTO embeddings (id) VALUES (1)")
    # and a pg-mode table still passes
    guard.check_pg_write("INSERT INTO watchlist (ticker) VALUES ('NVDA')")
