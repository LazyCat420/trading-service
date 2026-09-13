"""The RSS body-scrape deadline must cover the queue it has to traverse.

Measured 2026-09-12 on the live shape:

* `scraper_client` funnels EVERY `/scrape` and every feed `/collect` through one
  MODULE-LEVEL `asyncio.Semaphore(5)` keyed by the literal `"news"`, and the
  scraper service rate-limits 1 req/s per domain. One feed is one domain.
* `asyncio.wait_for(..., timeout=4.0)` starts its clock BEFORE that semaphore is
  acquired, so the wait for a permit is spent out of the fetch budget.
* Reproduced on an idle box (one feed, 15 URLs, semaphore 5, 4 s cap): bodies
  came back at 0.83 / 1.63 / 2.84 / 3.76 s and the other ELEVEN hit the 4.0 s
  cap. With 5 feeds contending for the same 5 permits the live log reads
  `article bodies scraped 0`.

The body is not cosmetic: `collect_feed` writes it into `news_articles.summary`
(`summary = body` when `len(body) >= 150`), and `summary` is the ONLY text field
that collection has — 0 of 140,159 rows carry `content`, `body`, `text` or
`description`. A body that never arrives is a row that keeps the RSS teaser.
"""

import asyncio
import time

import pytest

from app.collectors import news_collector as nc


class _FakeScraperClient:
    """Semaphore(5) + 1-req/s-per-domain, the two live constraints."""

    def __init__(self, permits: int = 5, domain_gap: float = 1.0, service_s: float = 0.05):
        self._sem = asyncio.Semaphore(permits)
        self._gap = domain_gap
        self._service_s = service_s
        self._next_free: dict[str, float] = {}
        self.calls = 0
        self.misses = 0
        self.failures = 0
        self.attempted: list[str] = []

    async def scrape(self, url, engine="http", options=None):
        self.calls += 1
        self.attempted.append(url)
        domain = url.split("/")[2]
        async with self._sem:
            now = time.monotonic()
            # Reserve the domain's next slot BEFORE sleeping. Reserving after
            # the sleep lets every waiter read the same free instant and wake
            # together, which silently removes the rate limit being modelled.
            free = max(self._next_free.get(domain, now), now)
            self._next_free[domain] = free + self._gap
            if free > now:
                await asyncio.sleep(free - now)
            await asyncio.sleep(self._service_s)
        return {"success": True, "content": "B" * 4000}


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeScraperClient()
    # `_scrape_article_body_via_service` imports the singleton inside the
    # function body, so the module attribute is the live seam.
    monkeypatch.setattr("app.services.scraper_client.scraper_client", client)
    return client


@pytest.mark.asyncio
async def test_default_deadline_covers_a_rate_limited_queue(fake_client):
    """Eight same-domain URLs at 1 req/s need ~8 s. The default must cover it.

    Red at the shipped `timeout=4.0`: four bodies, four fallbacks.
    """
    urls = [f"https://feed.example.com/a{i}" for i in range(8)]
    results = await asyncio.gather(*[
        nc._scrape_with_timeout(u, "teaser...", stats=None) for u in urls
    ])
    scraped = sum(1 for r in results if r.startswith("B"))
    assert scraped >= 7, (
        f"only {scraped}/8 bodies survived the deadline "
        f"(deadline={getattr(nc, 'SCRAPE_TIMEOUT_S', 4.0)}s)"
    )


@pytest.mark.asyncio
async def test_per_feed_fanout_does_not_oversubscribe_the_shared_semaphore():
    """A feed may not queue more scrapes than the shared Semaphore(5) can hold.

    FEED_CONCURRENCY feeds run at once and all of them share ONE Semaphore(5).
    `attempts_per_feed * FEED_CONCURRENCY` is the depth the deadline must cover,
    so the per-feed cap is what makes the deadline sizable at all.
    """
    assert nc.SCRAPE_ATTEMPTS_PER_FEED >= 1
    depth = nc.SCRAPE_ATTEMPTS_PER_FEED * nc.FEED_CONCURRENCY
    # At 1 req/s per domain over 5 permits the queue drains at ~5/s.
    assert depth / 5.0 < nc.SCRAPE_TIMEOUT_S, (
        f"worst-case queue depth {depth} drains in ~{depth / 5.0:.0f}s, "
        f"which the {nc.SCRAPE_TIMEOUT_S}s deadline does not cover"
    )


@pytest.mark.asyncio
async def test_breaker_stops_attempting_after_consecutive_misses(monkeypatch):
    """A dead scraper must stop costing every remaining feed a full deadline.

    And it must be counted LOCALLY: `wait_for` cancels `scraper_client.scrape`,
    `CancelledError` is a BaseException, so neither `misses` nor `failures`
    moves — `SweepRecord.total_miss` / `miss_rate` are blind to exactly this
    failure mode (worse: `calls` already incremented, so a timeout DILUTES
    miss_rate downward). The breaker cannot be built on that ledger.
    """
    breaker = nc._ScrapeBreaker(strikes=3)
    for _ in range(3):
        assert breaker.allow()
        breaker.record(ok=False)
    assert not breaker.allow(), "breaker did not trip after 3 consecutive misses"
    assert breaker.tripped

    fresh = nc._ScrapeBreaker(strikes=3)
    fresh.record(ok=False)
    fresh.record(ok=False)
    fresh.record(ok=True)      # one success resets the run
    fresh.record(ok=False)
    fresh.record(ok=False)
    assert fresh.allow(), "a success in the middle must reset the strike run"


@pytest.mark.asyncio
async def test_a_timed_out_scrape_is_invisible_to_the_sweep_ledger(fake_client):
    """Characterises WHY the breaker keeps its own counter."""
    slow = _FakeScraperClient(permits=1, domain_gap=0.0, service_s=5.0)
    import app.services.scraper_client as sc_mod
    original = sc_mod.scraper_client
    sc_mod.scraper_client = slow
    try:
        out = await nc._scrape_with_timeout(
            "https://x.example.com/a", "teaser...", timeout=0.2, stats=None
        )
    finally:
        sc_mod.scraper_client = original
    assert out == "teaser..."           # fell back
    assert slow.calls == 1              # the call WAS counted
    assert slow.misses == 0             # ...but not as a miss
    assert slow.failures == 0           # ...nor as a failure


@pytest.mark.asyncio
async def test_collect_feed_caps_its_scrape_fanout(monkeypatch):
    """End to end: one feed may not fire 15 scrapes into a 5-permit semaphore.

    Red before the fix: all twelve gated items were attempted.
    """
    from unittest.mock import MagicMock, patch

    items = [
        {
            "title": f"Gated story {i}",
            "url": f"https://feed.example.com/story{i}",
            "published_at": "2026-05-02T12:00:00Z",
            # >= 150 chars so the length arm does not fire, ending in the
            # ellipsis that DOES fire the gate — the live shape: 0 of the
            # newest 1,000 rss rows were under 150 chars, 114 carried "...".
            "summary": ("Shares moved after the company reported quarterly "
                        "results and guidance for the year ahead, analysts "
                        "said in a note to clients on Friday morning..."),
            "publisher": "Test Feed",
        }
        for i in range(12)
    ]

    client = _FakeScraperClient(domain_gap=0.0, service_s=0.0)

    async def _collect(source, req_data):
        return items

    client.collect = _collect
    monkeypatch.setattr("app.services.scraper_client.scraper_client", client)

    query = MagicMock()
    query.agg_row.return_value = (0,)
    query.find_rows.return_value = []
    query.find_row.return_value = None

    breaker = nc._ScrapeBreaker(strikes=99)   # isolate the CAP from the breaker
    with patch("app.db.mongo_store.upsert_doc", MagicMock()), \
         patch("app.collectors.news_collector.mongo_query", query), \
         patch("app.db.mongo_store.ensure_indexes", MagicMock()), \
         patch("app.db.mongo_store.count_docs", return_value=0), \
         patch("app.db.mongo_store.find_docs", return_value=[]), \
         patch("app.db.mongo_store.writes_mongo", lambda _t: True), \
         patch("app.db.mongo_store.writes_pg", lambda _t: False), \
         patch("app.db.mongo_store.reads_mongo", lambda _t: True):
        await nc.collect_feed("Test Feed", "https://feed.example.com/rss",
                              breaker=breaker)

    assert len(client.attempted) == nc.SCRAPE_ATTEMPTS_PER_FEED, (
        f"feed fired {len(client.attempted)} scrapes into a Semaphore(5); "
        f"cap is {nc.SCRAPE_ATTEMPTS_PER_FEED}"
    )
