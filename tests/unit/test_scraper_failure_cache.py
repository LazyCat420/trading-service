"""
Tests for permanent-failure memory and status honesty in the scraper.

Measured on the live trading DB over 14 days: 320 scrape failures, of which
234 (73%) were TWO urls — one article returning 410 since 2026-07-28, retried
157 times over 12 days, and one investors.com url retried 77 times. Nothing
remembered the answer, so every cycle re-walked every engine.

The domain those retries pointed at, thestockmarketwatch.com, was written up
as a source with a "0% scrape success rate" and proposed for removal. Measured
live it scrapes 5/5 of its current urls in under a second — the site had
retired its /news/ tree. These tests hold the line on both halves: a dead URL
is remembered, and a dead URL is not mistaken for a dead domain.
"""
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.scraper.core.base_result import ScrapeResult
from app.scraper.core.failure_cache import PERMANENT_STATUSES, FailureCache
from app.scraper.engines.auto_engine import AutoEngine


DEAD_URL = "https://thestockmarketwatch.com/news/stock-market-update-fomc-report-and-nvda-earnings/"
LIVE_URL = "https://thestockmarketwatch.com/digest"


def _result(status, content="x" * 400, success=None):
    return ScrapeResult(
        url=DEAD_URL,
        success=(200 <= status < 300) if success is None else success,
        content=content, data={},
        error=None if 200 <= status < 300 else f"HTTP {status}",
        engine_used="http", scraped_at=datetime.utcnow(), status_code=status,
    )


# ── The cache itself ─────────────────────────────────────────────────────────

def test_records_and_returns_the_reason():
    c = FailureCache()
    assert c.check(DEAD_URL) is None
    c.record(DEAD_URL, "HTTP 410")
    assert c.check(DEAD_URL) == "HTTP 410"


def test_entries_expire_so_a_restored_url_comes_back():
    """A site migration must not blacklist a url forever."""
    c = FailureCache(ttl_s=0.05)
    c.record(DEAD_URL, "HTTP 410")
    assert c.check(DEAD_URL) == "HTTP 410"
    time.sleep(0.06)
    assert c.check(DEAD_URL) is None


def test_cache_is_bounded_and_evicts_oldest_first():
    c = FailureCache(max_entries=3)
    for i in range(5):
        c.record(f"https://example.com/{i}", "HTTP 404")
    assert len(c) == 3
    assert c.check("https://example.com/0") is None, "oldest must be evicted"
    assert c.check("https://example.com/4") == "HTTP 404"


def test_only_permanent_statuses_are_in_the_set():
    """Transient failures must never be cached — that turns a blip into an
    outage. 5xx, 429 and timeouts all recover; 404/410 do not."""
    assert PERMANENT_STATUSES == {404, 410}
    for transient in (429, 500, 502, 503, 504, 403, 401):
        assert transient not in PERMANENT_STATUSES


# ── AutoEngine integration ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_410_is_not_escalated_to_playwright():
    """A browser cannot conjure a deleted page. This is the 16s that 317
    failures each paid before giving up."""
    engine = AutoEngine()
    http = AsyncMock(return_value=_result(410, content="This content has been permanently removed."))
    playwright = AsyncMock()

    with patch.object(engine.http_engine, "fetch", http), \
         patch.object(engine.playwright_engine, "fetch", playwright), \
         patch("app.scraper.engines.auto_engine.failure_cache", FailureCache()):
        res = await engine.fetch(DEAD_URL, {})

    assert res.success is False
    assert playwright.await_count == 0, "must not escalate a permanently-gone url"
    assert "permanently unavailable" in (res.error or "")


@pytest.mark.asyncio
async def test_the_second_attempt_never_reaches_the_network():
    """The 157-retry loop, as a test."""
    engine = AutoEngine()
    cache = FailureCache()
    http = AsyncMock(return_value=_result(410))

    with patch.object(engine.http_engine, "fetch", http), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        await engine.fetch(DEAD_URL, {})
        await engine.fetch(DEAD_URL, {})
        await engine.fetch(DEAD_URL, {})

    assert http.await_count == 1, "a known-dead url must be skipped, not refetched"


@pytest.mark.asyncio
async def test_a_dead_url_does_not_blacklist_its_domain():
    """thestockmarketwatch.com scrapes fine; one of its articles is 410. The
    cache is keyed by URL precisely so a live sibling is unaffected."""
    engine = AutoEngine()
    cache = FailureCache()

    async def _fetch(url, options):
        if url == DEAD_URL:
            return _result(410)
        # Article-length: the thin-content gate treats a short body as a
        # teaser, and this test is about URL-vs-domain scope, not length.
        return ScrapeResult(url=url, success=True, content="y" * 2000, data={},
                            error=None, engine_used="http",
                            scraped_at=datetime.utcnow(), status_code=200)

    with patch.object(engine.http_engine, "fetch", AsyncMock(side_effect=_fetch)), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        await engine.fetch(DEAD_URL, {})
        res = await engine.fetch(LIVE_URL, {})

    assert res.success is True, "a sibling url on the same domain must still scrape"
    assert cache.check(LIVE_URL) is None


@pytest.mark.asyncio
async def test_a_transient_failure_is_not_remembered():
    engine = AutoEngine()
    cache = FailureCache()
    http = AsyncMock(return_value=_result(503, content=""))
    playwright = AsyncMock(return_value=_result(503, content="", success=False))

    with patch.object(engine.http_engine, "fetch", http), \
         patch.object(engine.playwright_engine, "fetch", playwright), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        await engine.fetch(DEAD_URL, {})

    assert cache.check(DEAD_URL) is None, "5xx recovers — caching it self-inflicts an outage"
    assert playwright.await_count == 1, "transient failures must still escalate"


# ── HttpEngine status honesty ────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410, 403, 500, 503])
async def test_http_engine_does_not_report_an_error_page_as_success(status):
    """It returned success=True for ANY status. The 410 came back as
    success=True / status_code=410 / content="This content has been
    permanently removed." AutoEngine re-checked the status itself, but a direct
    engine="http" caller had only its own length guard between an error page
    and the article body."""
    from app.scraper.engines.http_engine import HttpEngine

    class _Resp:
        status_code = status
        headers = {"content-type": "text/html"}
        text = "<html><body>" + ("Access Denied. " * 40) + "</body></html>"

    class _Client:
        async def get(self, url):
            return _Resp()

    from app.scraper.core.session_manager import session_manager
    with patch.object(session_manager, "_client", _Client()):
        res = await HttpEngine().fetch("https://example.com/gone", {})

    assert res.success is False, f"HTTP {status} must not be a successful scrape"
    assert res.status_code == status
    assert res.error == f"HTTP {status}"


@pytest.mark.asyncio
async def test_http_engine_still_reports_2xx_as_success():
    from app.scraper.engines.http_engine import HttpEngine

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body>" + ("Nvidia rose three percent today. " * 20) + "</body></html>"

    class _Client:
        async def get(self, url):
            return _Resp()

    from app.scraper.core.session_manager import session_manager
    with patch.object(session_manager, "_client", _Client()):
        res = await HttpEngine().fetch("https://example.com/live", {})

    assert res.success is True
    assert res.error is None
    assert res.content


@pytest.mark.asyncio
async def test_auto_has_no_vision_phase():
    """Vision was removed; a failed scrape must end at playwright."""
    engine = AutoEngine()
    assert not hasattr(engine, "vision_engine")

    failed = ScrapeResult(url=DEAD_URL, success=False, content=None, data={},
                          error="boom", engine_used="playwright",
                          scraped_at=datetime.utcnow(), status_code=200)
    with patch.object(engine.http_engine, "fetch", AsyncMock(return_value=failed)), \
         patch.object(engine.playwright_engine, "fetch", AsyncMock(return_value=failed)), \
         patch("app.scraper.engines.auto_engine.failure_cache", FailureCache()):
        res = await engine.fetch(DEAD_URL, {})

    assert res.engine_used == "auto (failed)"
