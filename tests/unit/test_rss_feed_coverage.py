"""
Tests for RSS feed coverage.

Measured 2026-08-09: of 30 configured feeds, only **9 produced a single row in
30 days**. The cause was not broken feeds — `collect_all` ran serially with a
2s sleep between each, so 10 feeds cost ~30s, and both call sites passed
`limit_feeds=10`. That truncation was positional (the first 10 in dict order)
and silent, so 20 feeds were never fetched at all. Every foreign-language feed
sat past index 10, which is why non-English coverage was zero while the config
implied four sources.

Through the real collection path the never-fetched feeds were fine:

    BBC Business 50 items      Guardian Business 39     Les Echos FR 20
    Kiplinger 50               FT Markets 25            Handelsblatt DE 20
                               Federal Reserve 20

Only three were genuinely dead and are now removed: Benzinga (403 on both its
feed URLs, even with the full browser header set), Nikkei JP (403), and Sina
Finance CN (200 with zero items — the body is a corporate boilerplate stub).
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.collectors import news_collector as nc


ALL_FEEDS = len(nc.RSS_FEEDS) + len(nc.FOREIGN_RSS_FEEDS)


# ── Configuration ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("dead", ["Benzinga", "Nikkei JP", "Sina Finance CN"])
def test_the_measured_dead_feeds_are_gone(dead):
    assert dead not in nc.RSS_FEEDS
    assert dead not in nc.FOREIGN_RSS_FEEDS


def test_the_working_feeds_are_still_configured():
    """Removing dead feeds must not take live ones with them."""
    for live in ("BBC Business", "Kiplinger", "FT Markets", "Federal Reserve",
                 "Yahoo Finance", "CNBC Top"):
        assert live in nc.RSS_FEEDS, live


def test_foreign_coverage_survives():
    """Three of four foreign feeds were past the cap and two were dead, so
    non-English coverage was zero. What remains has to actually be there."""
    assert set(nc.FOREIGN_RSS_FEEDS) == {"Handelsblatt DE", "Les Echos FR"}


# ── Coverage ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_feed_is_fetched_by_default():
    """The defect: 20 of 30 feeds were never asked."""
    seen = []

    async def _feed(name, url, emit_cb=None, is_foreign=False):
        seen.append(name)
        return 1

    with patch.object(nc, "collect_feed", _feed):
        total = await nc.collect_all()

    assert len(seen) == ALL_FEEDS
    assert total == ALL_FEEDS
    assert "Les Echos FR" in seen, "foreign feeds sat past the old cap"


@pytest.mark.asyncio
async def test_neither_call_site_truncates_any_more():
    import inspect

    from app.services import flash_briefing, pipeline_service

    for mod in (pipeline_service, flash_briefing):
        src = inspect.getsource(mod)
        assert "collect_all(limit_feeds=10" not in src, mod.__name__


@pytest.mark.asyncio
async def test_a_limit_still_works_and_says_what_it_dropped(caplog):
    """A cap that is not reported reads as full coverage."""
    import logging

    async def _feed(name, url, emit_cb=None, is_foreign=False):
        return 1

    with patch.object(nc, "collect_feed", _feed), \
         caplog.at_level(logging.INFO, logger="app.collectors.news_collector"):
        total = await nc.collect_all(limit_feeds=3)

    assert total == 3
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "skipping" in msgs.lower()
    assert str(ALL_FEEDS - 3) in msgs


# ── Concurrency is what makes full coverage affordable ───────────────────────

@pytest.mark.asyncio
async def test_feeds_are_fetched_concurrently():
    """Serial + a 2s sleep each is why the cap existed. 27 feeds that way
    would cost ~80s of sleeping alone."""
    live = 0
    peak = 0

    async def _feed(name, url, emit_cb=None, is_foreign=False):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return 1

    with patch.object(nc, "collect_feed", _feed):
        await nc.collect_all()

    assert peak > 1, "feeds must not be fetched one at a time"
    assert peak <= nc.FEED_CONCURRENCY, f"ran {peak} against a limit of {nc.FEED_CONCURRENCY}"


@pytest.mark.asyncio
async def test_one_broken_feed_does_not_lose_the_others():
    async def _feed(name, url, emit_cb=None, is_foreign=False):
        if name == "Yahoo Finance":
            raise RuntimeError("403")
        return 1

    with patch.object(nc, "collect_feed", _feed):
        total = await nc.collect_all()

    assert total == ALL_FEEDS - 1


@pytest.mark.asyncio
async def test_no_sleep_between_feeds():
    """The 2s pace was per feed and serial; the scraper's own per-domain rate
    limiter is what should pace anything sharing a host."""
    async def _feed(name, url, emit_cb=None, is_foreign=False):
        return 0

    with patch.object(nc, "collect_feed", _feed):
        elapsed = asyncio.get_event_loop().time()
        await nc.collect_all()
        elapsed = asyncio.get_event_loop().time() - elapsed

    assert elapsed < 1.0, f"collect_all slept for {elapsed:.1f}s with no real work to do"


def test_feed_concurrency_does_not_exceed_the_client_semaphore():
    """`scraper_client` holds ONE semaphore of 5 for every call it makes, so a
    higher feed concurrency queues behind it and buys nothing. Measured over a
    full 27-feed pass on the container: 5 -> 105.0s, 10 -> 98.1s, 16 -> 102.8s.

    This guards against someone raising NEWS_FEED_CONCURRENCY expecting a
    speedup — the knob that would move it is the client semaphore, which also
    gates the article-body upgrade and every other scrape.
    """
    import inspect
    import re

    from app.services import scraper_client as sc

    src = inspect.getsource(sc.ScraperServiceClient._get_semaphore)
    m = re.search(r"asyncio\.Semaphore\((\d+)\)", src)
    assert m, "could not read the client semaphore limit"
    assert nc.FEED_CONCURRENCY <= int(m.group(1)), (
        f"FEED_CONCURRENCY={nc.FEED_CONCURRENCY} exceeds the client's "
        f"semaphore of {m.group(1)} — the extra parallelism cannot be used"
    )
