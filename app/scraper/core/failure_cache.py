"""
failure_cache.py — memory for URLs that are not worth fetching again.
---------------------------------------------------------------------
The scraper had no memory of a failure, so a dead URL was re-walked through
every engine on every cycle, forever. Measured over 14 days on the live
trading DB:

    domain                       attempts   distinct URLs   retries/URL
    thestockmarketwatch.com           157               1         157.0
    www.investors.com                  77               1          77.0
    seekingalpha.com                   70              59           1.2

234 of 320 scrape failures — 73% — were **two URLs**. The first is a single
article returning ``410 Gone`` since 2026-07-28, retried 157 times over 12
days. (seekingalpha's 1.2 shows what a genuine domain-level block looks like
by contrast: many distinct URLs, each tried about once.)

This is why the domain-level reading was wrong. thestockmarketwatch.com was
recorded as a source with a "0% scrape success rate" and proposed for
removal; measured live it scrapes 5/5 of its current URLs in under a second.
The site had simply retired its ``/news/`` tree. Pruning the domain would
have deleted a working source and left the retry loop running.

Scope: **permanent** failures only. A 404/410 means the URL is gone and no
amount of retrying changes that. Transient failures (timeouts, 5xx, rate
limits, bot-walls) are deliberately NOT cached — those do recover, and
caching them would turn a blip into a self-inflicted outage.

**Two layers, because there are two processes.** scraper-service runs
``uvicorn --workers 2`` (each worker boots its own Chromium), so an
in-process cache alone is learned twice: measured on the deployed container,
attempts 1 and 3 both hit the network at ~1.2-1.7s while attempt 2 was served
from memory at 0.01s. A dead URL therefore cost one fetch *per worker*, and a
restart threw the memory away entirely.

So an in-process LRU sits in front of a small SQLite file that every worker in
the container shares:

    check():  memory -> sqlite -> (miss) network
    record(): memory + sqlite

The memory layer keeps the hot path at ~0.01s; SQLite makes one worker's
discovery immediately visible to the other and survives a worker crash. The
store is optional — if the path cannot be opened (trading-service imports this
same module and has no ``/app/logs``), it degrades to memory-only with a
single warning rather than failing a scrape.

Point ``SCRAPER_FAILURE_CACHE_PATH`` at a mounted volume to keep the memory
across deploys; by default it lives inside the container and is re-learned
once per URL after a rebuild.
"""

import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

# HTTP statuses that mean "this URL is gone, and asking again will not help".
# 404 is included alongside 410 because a retired article path answers 404 far
# more often than the strictly-correct 410, and both were observed here.
PERMANENT_STATUSES = frozenset({404, 410})

# Long enough to kill the retry loop across many cycles, short enough that a
# URL restored by a site migration is picked back up the same day.
DEFAULT_TTL_S = 24 * 3600

# Bounded so a long-running container cannot grow this without limit; evicts
# oldest-first. Dead URLs are rare (2 accounted for 73% of failures), so this
# is generous.
DEFAULT_MAX_ENTRIES = 4096

# Shared by every worker in the container. /app/logs is created and chowned to
# the runtime user by scraper-service's Dockerfile. Point this at a mounted
# volume to keep the memory across deploys.
DEFAULT_STORE_PATH = os.getenv("SCRAPER_FAILURE_CACHE_PATH", "/app/logs/failure_cache.db")

# Expiries are stored as wall-clock (time.time), NOT time.monotonic: monotonic
# epochs are per-process, so two workers cannot compare each other's values.
_now = time.time


# SQLite's way of saying "someone else is writing, try again" — a TRANSIENT
# condition, and the normal state of affairs when two workers boot together.
_TRANSIENT_MARKERS = ("database is locked", "database table is locked",
                      "database is busy")


def _is_transient(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        m in str(exc).lower() for m in _TRANSIENT_MARKERS
    )


class SqliteFailureStore:
    """Cross-process store of dead URLs, shared by the container's workers.

    Optional by design: an unwritable path or a corrupt file disables the store
    and leaves the in-process cache doing its job. A scrape must never fail
    because its cache could not be written.

    But **a lock is not a corruption.** The first version disabled the store on
    *any* exception, and the deployed container immediately proved why that is
    wrong: both workers boot at once, one lost the race to create the schema,
    got ``database is locked``, and switched itself off for the life of the
    process. The shared cache shared nothing, and the measurement was
    unchanged at 2 fetches. Transient errors are now retried and never
    disable the store — the same distinction between permanent and transient
    that this module draws for HTTP statuses.
    """

    # A cold start with two workers racing on schema creation.
    _INIT_ATTEMPTS = 5
    _INIT_BACKOFF_S = 0.25

    def __init__(self, path: str):
        self.path = path
        self.enabled = False
        self._lock = threading.Lock()
        self._warned = False
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        except Exception as e:  # noqa: BLE001 — a bad path is permanent
            self._disable(e)
            return

        for attempt in range(self._INIT_ATTEMPTS):
            try:
                with self._connect() as conn:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS dead_urls ("
                        "  url TEXT PRIMARY KEY,"
                        "  reason TEXT NOT NULL,"
                        "  expires_at REAL NOT NULL)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS dead_urls_expires_at "
                        "ON dead_urls(expires_at)"
                    )
                self.enabled = True
                return
            except Exception as e:  # noqa: BLE001
                if _is_transient(e) and attempt < self._INIT_ATTEMPTS - 1:
                    time.sleep(self._INIT_BACKOFF_S * (attempt + 1))
                    continue
                self._disable(e)
                return

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        # busy_timeout FIRST. The original set journal_mode before it, so the
        # WAL switch itself ran with no busy handler and returned "database is
        # locked" the instant the other worker held the file.
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            # WAL lets the two workers read while the other writes. Switching
            # journal_mode needs a brief exclusive lock and sqlite does NOT
            # invoke the busy handler for it, so a loser here is expected and
            # harmless — the mode is a property of the file, and whoever won
            # already set it.
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        return conn

    def _disable(self, exc: Exception) -> None:
        """Turn the store off for good. Only for PERMANENT faults."""
        self.enabled = False
        if not self._warned:
            self._warned = True
            logger.warning(
                "[failure_cache] shared store unavailable at %s (%s) — "
                "falling back to in-process memory only", self.path, exc,
            )

    def _handle(self, exc: Exception, op: str) -> None:
        """Disable on a permanent fault; ride out a transient one."""
        if _is_transient(exc):
            logger.debug("[failure_cache] %s contended (%s) — keeping store", op, exc)
            return
        self._disable(exc)

    def record(self, url: str, reason: str, expires_at: float) -> None:
        if not self.enabled:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO dead_urls(url, reason, expires_at) VALUES (?,?,?) "
                    "ON CONFLICT(url) DO UPDATE SET reason=excluded.reason, "
                    "expires_at=excluded.expires_at",
                    (url, reason, expires_at),
                )
        except Exception as e:  # noqa: BLE001
            self._handle(e, "record")

    def check(self, url: str) -> tuple[str, float] | None:
        """Return ``(reason, expires_at)`` for a live entry, else None."""
        if not self.enabled:
            return None
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT reason, expires_at FROM dead_urls WHERE url = ?", (url,)
                ).fetchone()
            if row is None:
                return None
            reason, expires_at = row
            if _now() >= expires_at:
                self.forget(url)
                return None
            return reason, expires_at
        except Exception as e:  # noqa: BLE001
            self._handle(e, "check")
            return None

    def forget(self, url: str) -> None:
        if not self.enabled:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute("DELETE FROM dead_urls WHERE url = ?", (url,))
        except Exception as e:  # noqa: BLE001
            self._handle(e, "forget")

    def prune(self) -> int:
        """Drop expired rows. Returns how many went."""
        if not self.enabled:
            return 0
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute("DELETE FROM dead_urls WHERE expires_at <= ?", (_now(),))
                return cur.rowcount or 0
        except Exception as e:  # noqa: BLE001
            self._handle(e, "prune")
            return 0

    def clear(self) -> None:
        if not self.enabled:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute("DELETE FROM dead_urls")
        except Exception as e:  # noqa: BLE001
            self._handle(e, "clear")

    def __len__(self) -> int:
        if not self.enabled:
            return 0
        try:
            with self._lock, self._connect() as conn:
                return conn.execute(
                    "SELECT count(*) FROM dead_urls WHERE expires_at > ?", (_now(),)
                ).fetchone()[0]
        except Exception as e:  # noqa: BLE001
            self._handle(e, "len")
            return 0

    def __bool__(self) -> bool:
        """A store object always exists; emptiness is not absence.

        Without this, ``__len__`` decides truthiness and a FRESH (empty) store
        is falsy — so ``if self.store:`` silently skipped every write, and the
        very first record for any URL never reached disk. Caught by
        test_one_workers_discovery_protects_the_other.
        """
        return True


class FailureCache:
    """TTL + LRU memory of dead URLs, read-through to a cross-worker store."""

    # Prune expired rows at most this often, on a write.
    _PRUNE_INTERVAL_S = 600

    def __init__(self, ttl_s: float = DEFAULT_TTL_S,
                 max_entries: int = DEFAULT_MAX_ENTRIES,
                 store: "SqliteFailureStore | None" = None):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self.store = store
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._last_prune = _now()

    # ── internals ────────────────────────────────────────────────────────────

    def _remember(self, url: str, reason: str, expires_at: float) -> None:
        self._entries[url] = (expires_at, reason)
        self._entries.move_to_end(url)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def _maybe_prune(self) -> None:
        if self.store is not None and _now() - self._last_prune >= self._PRUNE_INTERVAL_S:
            self._last_prune = _now()
            gone = self.store.prune()
            if gone:
                logger.info("[failure_cache] pruned %d expired entries", gone)

    # ── api ──────────────────────────────────────────────────────────────────

    def record(self, url: str, reason: str) -> None:
        """Remember that ``url`` is permanently dead, for every worker."""
        expires_at = _now() + self.ttl_s
        self._remember(url, reason, expires_at)
        if self.store is not None:
            self.store.record(url, reason, expires_at)
            self._maybe_prune()
        logger.info("[failure_cache] %s marked dead (%s)", url, reason)

    def check(self, url: str) -> str | None:
        """Return the recorded reason if ``url`` is known-dead, else None.

        Memory first (the ~0.01s path), then the shared store — that second
        lookup is what stops worker B from re-fetching what worker A already
        proved dead.
        """
        entry = self._entries.get(url)
        if entry is not None:
            expires_at, reason = entry
            if _now() < expires_at:
                self._entries.move_to_end(url)
                return reason
            # Expired locally — drop it and fall through to the store, which
            # may hold a fresher entry written by the other worker.
            del self._entries[url]

        if self.store is not None:
            found = self.store.check(url)
            if found:
                reason, expires_at = found
                self._remember(url, reason, expires_at)
                return reason
        return None

    def clear(self) -> None:
        self._entries.clear()
        if self.store is not None:
            self.store.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        """Same trap as SqliteFailureStore.__bool__ — an empty cache is not
        an absent one."""
        return True


def _build_default_cache() -> FailureCache:
    """The process-wide cache, backed by the container-shared store."""
    return FailureCache(store=SqliteFailureStore(DEFAULT_STORE_PATH))


# Consulted by AutoEngine. One per process; the SQLite file behind it is one
# per container, so both uvicorn workers share what either of them learns.
failure_cache = _build_default_cache()
