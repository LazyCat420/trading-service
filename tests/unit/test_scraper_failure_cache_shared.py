"""
Tests for the CROSS-WORKER half of the failure cache.

scraper-service runs `uvicorn --workers 2`, each worker a separate process with
its own in-process cache. Measured on the deployed container before this fix,
hitting one dead URL five times:

    1.69s  0.01s  1.22s  0.01s  0.01s
    ^ worker A learns   ^ worker B learns

So a dead URL cost one network fetch PER WORKER, and a restart threw the whole
memory away. The in-process tests in test_scraper_failure_cache.py cannot see
this: a single FailureCache instance is always consistent with itself. These
tests use *separate instances over one file* — the same thing two workers do.
"""
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.scraper.core.failure_cache import FailureCache, SqliteFailureStore


DEAD = "https://thestockmarketwatch.com/news/stock-market-update-fomc-report-and-nvda-earnings/"


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "failure_cache.db")


def _worker(store_path, **kw):
    """A cache as a fresh uvicorn worker would build it."""
    return FailureCache(store=SqliteFailureStore(store_path), **kw)


# ── The defect this fix exists for ───────────────────────────────────────────

def test_one_workers_discovery_protects_the_other(store_path):
    """The 1.22s on attempt 3. Worker B must not re-fetch what A proved dead."""
    worker_a = _worker(store_path)
    worker_b = _worker(store_path)

    assert worker_b.check(DEAD) is None, "nothing known yet"
    worker_a.record(DEAD, "HTTP 410")

    assert worker_b.check(DEAD) == "HTTP 410", \
        "worker B must see worker A's discovery without touching the network"


def test_the_second_worker_serves_from_memory_after_one_store_read(store_path):
    """The store read happens once per worker; after that it is the hot path."""
    worker_a = _worker(store_path)
    worker_b = _worker(store_path)
    worker_a.record(DEAD, "HTTP 410")

    assert worker_b.check(DEAD) == "HTTP 410"   # promotes into B's memory
    worker_b.store.enabled = False              # store now unreachable
    assert worker_b.check(DEAD) == "HTTP 410", "must be served from memory"


def test_a_restarted_worker_still_remembers(store_path):
    """A worker crash or reload must not re-learn every dead URL."""
    _worker(store_path).record(DEAD, "HTTP 404")
    assert _worker(store_path).check(DEAD) == "HTTP 404"


def test_a_live_url_is_never_reported_dead_across_workers(store_path):
    worker_a = _worker(store_path)
    worker_b = _worker(store_path)
    worker_a.record(DEAD, "HTTP 410")
    assert worker_b.check("https://thestockmarketwatch.com/digest") is None


# ── TTL across the boundary ──────────────────────────────────────────────────

def test_expiry_is_honoured_by_a_worker_that_never_recorded_it(store_path):
    """Wall-clock, not monotonic: monotonic epochs differ per process, so a
    shared expiry written by one worker would be meaningless to another."""
    worker_a = _worker(store_path, ttl_s=0.05)
    worker_a.record(DEAD, "HTTP 410")
    time.sleep(0.06)

    assert _worker(store_path).check(DEAD) is None, "a migrated URL must come back"


def test_an_expired_local_entry_falls_through_to_a_fresher_shared_one(store_path):
    """B's copy expires; A has since re-recorded. B must pick up A's entry
    rather than reporting the URL live and re-fetching it."""
    worker_a = _worker(store_path)
    worker_b = _worker(store_path, ttl_s=0.05)

    worker_a.record(DEAD, "HTTP 410")
    assert worker_b.check(DEAD) == "HTTP 410"
    time.sleep(0.06)  # B's local copy is stale, A's shared entry is not

    assert worker_b.check(DEAD) == "HTTP 410"


def test_prune_drops_only_expired_rows(store_path):
    store = SqliteFailureStore(store_path)
    store.record("https://example.com/gone", "HTTP 410", time.time() - 1)
    store.record("https://example.com/still-dead", "HTTP 404", time.time() + 3600)

    assert store.prune() == 1
    assert store.check("https://example.com/gone") is None
    assert store.check("https://example.com/still-dead")[0] == "HTTP 404"


# ── Degradation: this module is imported by trading-service too ──────────────

def test_an_unwritable_path_degrades_to_memory_only(tmp_path):
    """trading-service imports app.scraper and has no /app/logs. A cache that
    cannot open its file must still work, not break every scrape."""
    blocked = tmp_path / "nope"
    blocked.write_text("this is a file, not a directory")
    cache = _worker(str(blocked / "sub" / "cache.db"))

    assert cache.store.enabled is False
    cache.record(DEAD, "HTTP 410")
    assert cache.check(DEAD) == "HTTP 410", "memory must still work"


def test_a_corrupt_store_degrades_instead_of_raising(store_path):
    """A damaged cache file must cost a re-fetch, never a crashed scrape.

    Note the -wal/-shm sidecars have to go too: in WAL mode the committed rows
    live in the sidecar, so overwriting only the main file leaves the data
    perfectly readable — the first version of this test "passed" corruption
    that had not happened."""
    cache = _worker(store_path)
    cache.record(DEAD, "HTTP 410")

    for suffix in ("-wal", "-shm"):
        if os.path.exists(store_path + suffix):
            os.remove(store_path + suffix)
    with open(store_path, "wb") as fh:
        fh.write(b"not a database at all" * 100)

    other = _worker(store_path)
    other._entries.clear()

    assert other.check(DEAD) is None, "degraded — but it must not raise"
    other.record(DEAD, "HTTP 410")
    assert other.check(DEAD) == "HTTP 410", "memory must still serve"


def test_a_readonly_store_disables_itself_without_raising(store_path):
    cache = _worker(store_path)
    os.chmod(store_path, 0o400)
    try:
        cache.record(DEAD, "HTTP 410")      # write fails internally
        assert cache.check(DEAD) == "HTTP 410"
    finally:
        os.chmod(store_path, 0o600)


# ── Concurrency: two workers do write at the same time ───────────────────────

def test_concurrent_writes_all_land(store_path):
    """WAL + busy_timeout. Without them a simultaneous write returns
    "database is locked" and the entry is silently lost."""
    caches = [_worker(store_path) for _ in range(4)]
    urls = [f"https://example.com/dead-{i}" for i in range(40)]

    def _write(i):
        caches[i % len(caches)].record(urls[i], "HTTP 410")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, range(len(urls))))

    reader = _worker(store_path)
    missing = [u for u in urls if reader.check(u) is None]
    assert not missing, f"{len(missing)} entries lost under concurrent writes"


def test_wal_mode_is_actually_set(store_path):
    SqliteFailureStore(store_path)
    with sqlite3.connect(store_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# ── A lock is not a corruption ───────────────────────────────────────────────

def test_a_transient_lock_does_not_disable_the_store(store_path):
    """The defect the deployed container caught. Both workers boot together,
    one loses the schema-creation race, gets "database is locked" — and the
    first version switched itself off for the life of the process, so the
    shared cache shared nothing and the measurement stayed at 2 fetches."""
    store = SqliteFailureStore(store_path)
    store._handle(sqlite3.OperationalError("database is locked"), "record")

    assert store.enabled is True, "a lock is transient; it must not disable the store"


def test_a_real_fault_still_disables_the_store(store_path):
    """The other half — without this, the test above passes for both states."""
    store = SqliteFailureStore(store_path)
    store._handle(sqlite3.DatabaseError("file is not a database"), "check")

    assert store.enabled is False


def test_init_survives_a_contended_cold_start(store_path):
    """Every worker must come up enabled even when they all boot at once."""
    stores = []

    def _boot(_):
        stores.append(SqliteFailureStore(store_path))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_boot, range(8)))

    disabled = [s for s in stores if not s.enabled]
    assert not disabled, f"{len(disabled)} of {len(stores)} workers came up disabled"


def test_busy_timeout_is_set_before_the_wal_switch(store_path):
    """Ordering was the bug: journal_mode ran with no busy handler, so it
    returned "database is locked" the instant another worker held the file."""
    store = SqliteFailureStore(store_path)
    conn = store._connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000
    finally:
        conn.close()


def test_a_worker_that_lost_the_wal_race_still_reads_and_writes(store_path):
    """Losing the journal_mode switch is harmless — the mode belongs to the
    file, and the winner already set it."""
    first = SqliteFailureStore(store_path)
    first.record(DEAD, "HTTP 410", time.time() + 3600)

    holder = first._connect()          # hold a connection open
    try:
        late = SqliteFailureStore(store_path)
        assert late.enabled is True
        assert late.check(DEAD)[0] == "HTTP 410"
        late.record("https://example.com/other", "HTTP 404", time.time() + 3600)
        assert late.check("https://example.com/other")[0] == "HTTP 404"
    finally:
        holder.close()


def test_an_empty_store_is_truthy(store_path):
    """The bug this fix nearly shipped with. `__len__` on the store made a
    FRESH store falsy, so `if self.store:` skipped the write and the first
    record for any URL never reached disk — every worker re-learned every URL,
    which is the exact defect the shared store exists to remove."""
    store = SqliteFailureStore(store_path)
    assert len(store) == 0
    assert bool(store) is True, "an empty store must not read as an absent one"
    assert bool(FailureCache()) is True


def test_recording_the_same_url_twice_updates_rather_than_duplicating(store_path):
    store = SqliteFailureStore(store_path)
    store.record(DEAD, "HTTP 404", time.time() + 3600)
    store.record(DEAD, "HTTP 410", time.time() + 3600)

    assert len(store) == 1
    assert store.check(DEAD)[0] == "HTTP 410"
