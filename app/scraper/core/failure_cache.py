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

In-process and per-container by design: it protects every caller of
``/scrape`` with no schema change and no cross-service coupling. A restart
re-learns within one cycle, which is cheap — the cost being removed is 157
retries over 12 days, not 157 retries in an hour.
"""

import logging
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


class FailureCache:
    """TTL + LRU memory of permanently-dead URLs."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def record(self, url: str, reason: str) -> None:
        """Remember that ``url`` is permanently dead."""
        self._entries[url] = (time.monotonic() + self.ttl_s, reason)
        self._entries.move_to_end(url)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        logger.info("[failure_cache] %s marked dead (%s)", url, reason)

    def check(self, url: str) -> str | None:
        """Return the recorded reason if ``url`` is known-dead, else None."""
        entry = self._entries.get(url)
        if entry is None:
            return None
        expires_at, reason = entry
        if time.monotonic() >= expires_at:
            # Expired — drop it and let the caller try again.
            del self._entries[url]
            return None
        self._entries.move_to_end(url)
        return reason

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


# Shared instance — one memory per process, consulted by AutoEngine.
failure_cache = FailureCache()
