"""The failure cache now outlives the container, so it needs a lifecycle.

Until 3e5651c this database died with every rebuild, which hid three things:
new code never met an old schema, nothing ever had to be evicted, and a store
that switched itself off was reset within the day. On a named volume all three
became real.
"""
import sqlite3
import time

import pytest

from app.scraper.core import content_quality
from app.scraper.core.failure_cache import (
    FailureCache, SqliteFailureStore, _is_transient,
)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "fc.db")


# ── Migration ────────────────────────────────────────────────────────────────

def test_an_old_database_is_migrated_not_disabled(db):
    """The pre-3e5651c schema, as it exists on the live volume today."""
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE dead_urls (url TEXT PRIMARY KEY, reason TEXT NOT NULL, expires_at REAL NOT NULL)")
    conn.execute("CREATE TABLE domain_quality ("
                 " domain TEXT PRIMARY KEY, thin_streak INTEGER NOT NULL DEFAULT 0,"
                 " good_count INTEGER NOT NULL DEFAULT 0, last_thin_at REAL NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO domain_quality VALUES ('www.investors.com', 19, 0, 1756900000.0)")
    conn.commit(); conn.close()

    store = SqliteFailureStore(db)

    assert store.enabled, "an old schema must migrate, not disable the shared store"
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(domain_quality)")}
    assert "last_seen" in cols
    assert store.quality_of("www.investors.com")[:2] == (19, 0), "learned history survives"


def test_migration_is_idempotent(db):
    SqliteFailureStore(db)
    again = SqliteFailureStore(db)
    assert again.enabled
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == SqliteFailureStore._SCHEMA_VERSION


def test_a_database_from_a_newer_build_is_left_alone(db, caplog):
    """Two workers, one mid-deploy: the new one must not downgrade the file."""
    SqliteFailureStore(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99"); conn.commit(); conn.close()

    store = SqliteFailureStore(db)
    assert store.enabled
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 99


# ── Transient vs permanent ───────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "database is locked",
    "disk I/O error",                 # a Synology remount
    "unable to open database file",   # the -wal sidecar, momentarily
    "database or disk is full",
])
def test_a_transient_fault_keeps_the_store(msg):
    """Each of these appears on a NAS volume and passes. Only the first was in
    the old marker list; the other three disabled the store permanently."""
    assert _is_transient(sqlite3.OperationalError(msg)) is True


@pytest.mark.parametrize("msg", [
    "attempt to write a readonly database",
    "no such column: last_seen",
    "file is not a database",
])
def test_a_permanent_fault_still_disables_the_store(msg):
    """The other direction — without this, 'treat everything as transient'
    would satisfy every test above and retry a broken file forever."""
    assert _is_transient(sqlite3.OperationalError(msg)) is False


def test_corruption_is_never_transient():
    assert _is_transient(sqlite3.DatabaseError("database disk image is malformed")) is False


# ── Eviction ─────────────────────────────────────────────────────────────────

def test_a_stale_domain_row_is_evicted(db):
    """domain_quality had no eviction at all: no code anywhere deleted from it,
    so it grew monotonically with every domain ever scraped."""
    store = SqliteFailureStore(db)
    store.record_quality("ancient.example.com", good=False, now=time.time() - (200 * 24 * 3600))
    store.record_quality("current.example.com", good=False, now=time.time())

    removed = store.prune(domain_max_age_s=90 * 24 * 3600)

    assert removed >= 1
    assert store.quality_of("ancient.example.com") is None
    assert store.quality_of("current.example.com") is not None, "a live domain must keep its history"


def test_pruning_is_reachable_from_the_read_path(db, monkeypatch):
    """_maybe_prune ran only from record(), which fires on a 404/410 — 8 rows in
    14 days — so it was effectively never reached."""
    cache = FailureCache(store=SqliteFailureStore(db))
    cache._last_prune = 0.0            # interval elapsed
    calls = []
    monkeypatch.setattr(cache.store, "prune", lambda *a, **k: calls.append(1) or 0)

    cache.check("https://example.com/never-recorded")

    assert calls, "a read must be able to trigger the interval-guarded prune"


def test_a_good_response_stamps_last_seen(db):
    """Eviction reads last_seen; without a producer it would evict every row at
    once — a field with no writer reports a confident zero."""
    store = SqliteFailureStore(db)
    store.record_quality("example.com", good=True, now=1_800_000_000.0)
    row = sqlite3.connect(db).execute(
        "SELECT last_seen FROM domain_quality WHERE domain='example.com'").fetchone()
    assert row[0] == 1_800_000_000.0


def test_the_skip_rule_is_unchanged_by_any_of_this(db):
    """seekingalpha is why the three-part rule exists (bimodal: thin_streak 10,
    good_count 10). Regression guard on the behaviour that matters."""
    cache = FailureCache(store=SqliteFailureStore(db))
    for _ in range(content_quality.SKIP_AFTER_THIN):
        cache.record_quality("thin.example.com", good=False)
    assert cache.should_skip_domain("thin.example.com") is not None

    cache.record_quality("thin.example.com", good=True)
    assert cache.should_skip_domain("thin.example.com") is None, \
        "one good article must clear the streak"
