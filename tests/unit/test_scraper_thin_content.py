"""
Tests for the thin-content gate and the earned domain skip.

A scrape can return 200, clear every block signature, and still be a headline
plus one lede paragraph. Stored, that teaser is indistinguishable from a full
article — a silent failure, worse than an error. Measured live through the
deployed scraper, 6 URLs per domain (2026-08-09):

    www.investors.com     median  526   usable 0/6   never delivers
    seekingalpha.com      median   35   usable 1/6
    www.bloomberg.com     median  585   usable 1/6
    www.marketwatch.com   median  623   usable 1/6
    www.cnbc.com          median 1044   usable 4/6
    www.tradingview.com   median 1472   usable 3/6
    www.stocktitan.net    median  329   usable 2/6   329 or 3000+, nothing between
    www.fool.com          median 12035  usable 6/6

The populations leave a clean gap — thin tops out at 897, full starts at 1044 —
so the cut is 900. Four domains are bimodal, which is why the gate judges the
RESPONSE and the skip list is earned per domain rather than hardcoded.
"""
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.scraper.core import content_quality
from app.scraper.core.base_result import ScrapeResult
from app.scraper.core.failure_cache import FailureCache, SqliteFailureStore
from app.scraper.engines.auto_engine import AutoEngine

IBD = "https://www.investors.com/market-trend/stock-market-today/some-article/"
TITAN_THIN = "https://www.stocktitan.net/news/AAPL/thin"
TITAN_GOOD = "https://www.stocktitan.net/news/AAPL/real"


@pytest.fixture
def cache(tmp_path):
    return FailureCache(store=SqliteFailureStore(str(tmp_path / "fc.db")))


def _ok(url, chars):
    return ScrapeResult(
        url=url, success=True, content="word " * (chars // 5), data={},
        error=None, engine_used="http", scraped_at=datetime.utcnow(),
        status_code=200,
    )


# ── The threshold sits in the measured gap ───────────────────────────────────

@pytest.mark.parametrize("chars", [35, 329, 526, 585, 623, 745, 811, 848, 897])
def test_measured_teasers_are_thin(chars):
    assert content_quality.is_thin("x" * chars) is True


@pytest.mark.parametrize("chars", [1044, 1241, 1275, 1472, 2443, 5277, 12035])
def test_measured_articles_are_not_thin(chars):
    assert content_quality.is_thin("x" * chars) is False


def test_the_cut_lies_between_the_two_populations():
    """897 was the largest teaser and 1044 the smallest article."""
    assert 897 < content_quality.MIN_ARTICLE_CHARS <= 1044


# ── The gate ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_teaser_is_a_failure_not_an_article(cache):
    """IBD returns 200 + ~526 chars and it was stored as the article body."""
    engine = AutoEngine()
    with patch.object(engine.http_engine, "fetch", AsyncMock(return_value=_ok(IBD, 526))), \
         patch.object(engine.playwright_engine, "fetch", AsyncMock(return_value=_ok(IBD, 526))), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        res = await engine.fetch(IBD, {})

    assert res.success is False
    assert res.engine_used == "auto (thin)"
    assert "thin content" in (res.error or "")


@pytest.mark.asyncio
async def test_a_teaser_does_not_escalate_to_playwright(cache):
    """A browser cannot unlock a paywall — escalating only costs time."""
    engine = AutoEngine()
    playwright = AsyncMock()
    with patch.object(engine.http_engine, "fetch", AsyncMock(return_value=_ok(IBD, 526))), \
         patch.object(engine.playwright_engine, "fetch", playwright), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        await engine.fetch(IBD, {})

    assert playwright.await_count == 0


@pytest.mark.asyncio
async def test_a_real_article_passes_untouched(cache):
    engine = AutoEngine()
    with patch.object(engine.http_engine, "fetch", AsyncMock(return_value=_ok(TITAN_GOOD, 3266))), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        res = await engine.fetch(TITAN_GOOD, {})

    assert res.success is True
    assert res.engine_used == "auto (http)"


# ── The earned skip ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_domain_that_only_ever_teases_stops_being_fetched(cache):
    """investors.com: 0/6 usable. After the streak it costs no request."""
    engine = AutoEngine()
    http = AsyncMock(return_value=_ok(IBD, 526))
    with patch.object(engine.http_engine, "fetch", http), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        for _ in range(content_quality.SKIP_AFTER_THIN):
            await engine.fetch(IBD, {})
        calls_before = http.await_count
        res = await engine.fetch(IBD, {})

    assert http.await_count == calls_before, "must not fetch a domain that never delivers"
    assert res.success is False
    assert "yields no articles" in (res.error or "")


@pytest.mark.asyncio
async def test_one_good_article_protects_a_bimodal_domain_forever(cache):
    """stocktitan returns 329 or 3000+. A domain-level denylist would throw
    away its real articles; a single good response must immunise it."""
    engine = AutoEngine()

    async def _fetch(url, options):
        return _ok(url, 3266 if url == TITAN_GOOD else 329)

    http = AsyncMock(side_effect=_fetch)
    with patch.object(engine.http_engine, "fetch", http), \
         patch.object(engine.playwright_engine, "fetch", http), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        await engine.fetch(TITAN_GOOD, {})                     # one real article
        for _ in range(content_quality.SKIP_AFTER_THIN + 3):   # then a long thin run
            await engine.fetch(TITAN_THIN, {})
        calls_before = http.await_count
        res = await engine.fetch(TITAN_GOOD, {})

    assert http.await_count > calls_before, "a domain with real articles must keep being fetched"
    assert res.success is True


def test_a_good_response_clears_the_thin_streak(cache):
    dom = "www.stocktitan.net"
    for _ in range(content_quality.SKIP_AFTER_THIN):
        cache.record_quality(dom, good=False)
    assert cache.should_skip_domain(dom) is not None

    cache.record_quality(dom, good=True)
    assert cache.should_skip_domain(dom) is None


def test_a_skipped_domain_is_retried_after_the_ttl(cache, monkeypatch):
    """A site that stops paywalling recovers without anyone editing a list."""
    dom = "www.investors.com"
    for _ in range(content_quality.SKIP_AFTER_THIN):
        cache.record_quality(dom, good=False)
    assert cache.should_skip_domain(dom) is not None

    later = time.time() + content_quality.SKIP_TTL_S + 1
    monkeypatch.setattr("app.scraper.core.failure_cache._now", lambda: later)
    assert cache.should_skip_domain(dom) is None, "one probe must be allowed through"


def test_a_short_thin_run_does_not_skip(cache):
    """cnbc is 4/6 — a couple of teasers must not cost it the domain."""
    dom = "www.cnbc.com"
    for _ in range(content_quality.SKIP_AFTER_THIN - 1):
        cache.record_quality(dom, good=False)
    assert cache.should_skip_domain(dom) is None


def test_the_skip_is_shared_between_workers(tmp_path):
    """Worker B must not re-learn a domain worker A already wrote off."""
    path = str(tmp_path / "fc.db")
    worker_a = FailureCache(store=SqliteFailureStore(path))
    worker_b = FailureCache(store=SqliteFailureStore(path))

    for _ in range(content_quality.SKIP_AFTER_THIN):
        worker_a.record_quality("www.investors.com", good=False)

    assert worker_b.should_skip_domain("www.investors.com") is not None


def test_quality_tracking_is_a_noop_without_a_store():
    """trading-service imports this module with no writable path."""
    cache = FailureCache()
    cache.record_quality("www.investors.com", good=False)
    assert cache.should_skip_domain("www.investors.com") is None


def test_domain_of_handles_a_malformed_url():
    assert content_quality.domain_of("not a url") == ""
    assert content_quality.domain_of("https://www.investors.com/x") == "www.investors.com"


# ── A rate limit is not a paywall ────────────────────────────────────────────

RATE_LIMIT_PAGE = (
    "Just a moment 429 Too Many Requests You're loading pages faster than our "
    "rate limits allow. This is a brief, automatic cooldown, not a block. "
    "Please wait about 4 minutes before continuing. Back to Previous Page Home Page"
)


def test_a_rate_limit_page_is_detected_as_blocked():
    """Verbatim from stocktitan.net under our own probing. Short enough to
    read as a teaser, which would have counted toward the domain skip."""
    assert AutoEngine().is_blocked_content(RATE_LIMIT_PAGE) is True


@pytest.mark.asyncio
async def test_our_own_rate_limiting_cannot_earn_a_domain_a_skip(cache):
    """The whole failure mode: probe a working site too fast, get 429s, and
    the gate writes it off for 24h. stocktitan is 4/6 usable — it must not be
    skippable by our request rate."""
    engine = AutoEngine()
    limited = ScrapeResult(
        url=TITAN_GOOD, success=True, content=RATE_LIMIT_PAGE, data={},
        error=None, engine_used="http", scraped_at=datetime.utcnow(),
        status_code=429,
    )
    with patch.object(engine.http_engine, "fetch", AsyncMock(return_value=limited)), \
         patch.object(engine.playwright_engine, "fetch", AsyncMock(return_value=limited)), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        for _ in range(content_quality.SKIP_AFTER_THIN + 3):
            await engine.fetch(TITAN_GOOD, {})

    assert cache.should_skip_domain("www.stocktitan.net") is None, \
        "a rate limit must never count toward the domain skip"


@pytest.mark.asyncio
async def test_a_non_2xx_thin_body_does_not_count_against_the_domain(cache):
    """Playwright reports no status, so None must still count; an explicit
    5xx/429 must not."""
    engine = AutoEngine()
    refused = ScrapeResult(
        url=TITAN_THIN, success=True, content="short body " * 20, data={},
        error=None, engine_used="playwright", scraped_at=datetime.utcnow(),
        status_code=503,
    )
    failed_http = ScrapeResult(
        url=TITAN_THIN, success=False, content=None, data={}, error="boom",
        engine_used="http", scraped_at=datetime.utcnow(), status_code=503,
    )
    with patch.object(engine.http_engine, "fetch", AsyncMock(return_value=failed_http)), \
         patch.object(engine.playwright_engine, "fetch", AsyncMock(return_value=refused)), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        for _ in range(content_quality.SKIP_AFTER_THIN + 2):
            await engine.fetch(TITAN_THIN, {})

    assert cache.should_skip_domain("www.stocktitan.net") is None


@pytest.mark.asyncio
async def test_a_served_200_teaser_still_counts(cache):
    """The other side — without this the guard above would disarm the skip."""
    engine = AutoEngine()
    with patch.object(engine.http_engine, "fetch",
                      AsyncMock(return_value=_ok(IBD, 526))), \
         patch("app.scraper.engines.auto_engine.failure_cache", cache):
        for _ in range(content_quality.SKIP_AFTER_THIN):
            await engine.fetch(IBD, {})

    assert cache.should_skip_domain("www.investors.com") is not None
