"""
Tests for the provider-blurb body upgrade (news_api_rotator._upgrade_bodies).

The body scrape fired only when the API summary was under 150 chars. Providers
return 300-500, so it never fired — while an article needs ~900 to be worth
reading. Measured over 30 days:

    source          rows   under 900   avg chars
    finnhub       11,102        98%          303
    alphavantage   4,658        99%          471
    polygon        3,060       100%          457

Scraping a sample of those blurbs upgraded 17 of 24 (70%) into real articles:
494 -> 6,058 chars, 482 -> 12,458. ~13k articles a month were stored as
headlines because nothing asked for the body.

The trigger could not simply be raised: `_persist_articles` awaited each
scrape serially INSIDE an open DB transaction, and a run carries up to
10 providers x 10 articles. These tests hold both halves — the upgrade
happens, and it stays bounded.
"""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.collectors import news_api_rotator as nar
from app.collectors.news_api_rotator import NewsArticle


def _article(url, summary, title="A headline"):
    return NewsArticle(title=title, url=url, summary=summary, source="polygon",
                       published_at=datetime(2026, 8, 9, tzinfo=UTC))


def _patch_scraper(fn):
    """`_upgrade_bodies` imports the scraper helper lazily, from news_collector."""
    return patch("app.collectors.news_collector._scrape_article_body_via_service", fn)


# ── The upgrade itself ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_provider_blurb_is_upgraded_to_the_real_article():
    """457 chars in, 4,834 out — the measured polygon case."""
    arts = [_article("https://x.test/a", "b" * 457)]
    with _patch_scraper(AsyncMock(return_value="w" * 4834)):
        bodies = await nar._upgrade_bodies(arts)

    assert bodies["https://x.test/a"] == "w" * 4834


@pytest.mark.asyncio
async def test_the_old_150_threshold_would_have_skipped_it():
    """Guards the actual defect: 457 > 150, so nothing was ever requested."""
    assert 150 < 457 < nar.MIN_BODY_CHARS
    arts = [_article("https://x.test/a", "b" * 457)]
    scraper = AsyncMock(return_value="w" * 4834)
    with _patch_scraper(scraper):
        await nar._upgrade_bodies(arts)

    assert scraper.await_count == 1, "a 457-char blurb must now be asked about"


@pytest.mark.asyncio
async def test_an_article_that_is_already_long_enough_is_not_refetched():
    arts = [_article("https://x.test/a", "b" * 4000)]
    scraper = AsyncMock(return_value="w" * 5000)
    with _patch_scraper(scraper):
        bodies = await nar._upgrade_bodies(arts)

    assert scraper.await_count == 0
    assert bodies == {}


@pytest.mark.asyncio
async def test_a_truncated_summary_is_upgraded_even_when_long():
    arts = [_article("https://x.test/a", "b" * 2000 + "...")]
    with _patch_scraper(AsyncMock(return_value="w" * 4000)):
        bodies = await nar._upgrade_bodies(arts)

    assert bodies


@pytest.mark.asyncio
async def test_a_thin_scrape_is_not_accepted_as_an_upgrade():
    """The scraper's own gate refuses sub-900 bodies; asking for one and then
    storing it anyway would reintroduce the teaser problem here."""
    arts = [_article("https://x.test/a", "b" * 457)]
    with _patch_scraper(AsyncMock(return_value="w" * 300)):
        bodies = await nar._upgrade_bodies(arts)

    assert bodies == {}


@pytest.mark.asyncio
async def test_one_url_is_attempted_once_even_across_several_articles():
    """The same story arrives from several providers; the fan-out must not
    multiply the fetches."""
    arts = [_article("https://x.test/same", "b" * 300) for _ in range(5)]
    scraper = AsyncMock(return_value="w" * 3000)
    with _patch_scraper(scraper):
        await nar._upgrade_bodies(arts)

    assert scraper.await_count == 1


# ── Bounded, because a run carries ~100 articles ─────────────────────────────

@pytest.mark.asyncio
async def test_attempts_are_capped(monkeypatch):
    monkeypatch.setattr(nar, "UPGRADE_LIMIT", 10)
    arts = [_article(f"https://x.test/{i}", "b" * 300) for i in range(100)]
    scraper = AsyncMock(return_value="w" * 3000)
    with _patch_scraper(scraper):
        await nar._upgrade_bodies(arts)

    assert scraper.await_count == 10, "100 sequential fetches per ticker is the thing being prevented"


@pytest.mark.asyncio
async def test_the_shortest_summaries_are_attempted_first(monkeypatch):
    """Priority is where the most is gained — a 160-char blurb over a 880."""
    monkeypatch.setattr(nar, "UPGRADE_LIMIT", 2)
    arts = [
        _article("https://x.test/long", "b" * 880),
        _article("https://x.test/short", "b" * 160),
        _article("https://x.test/mid", "b" * 500),
    ]
    seen = []

    async def _scrape(url):
        seen.append(url)
        return "w" * 3000

    with _patch_scraper(_scrape):
        await nar._upgrade_bodies(arts)

    assert set(seen) == {"https://x.test/short", "https://x.test/mid"}


@pytest.mark.asyncio
async def test_concurrency_is_limited(monkeypatch):
    """The per-domain rate limiter should be respected, not fought."""
    monkeypatch.setattr(nar, "UPGRADE_CONCURRENCY", 3)
    monkeypatch.setattr(nar, "UPGRADE_LIMIT", 50)
    live = 0
    peak = 0

    async def _scrape(url):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return "w" * 3000

    arts = [_article(f"https://x.test/{i}", "b" * 300) for i in range(20)]
    with _patch_scraper(_scrape):
        await nar._upgrade_bodies(arts)

    assert peak <= 3, f"ran {peak} concurrent scrapes against a limit of 3"


@pytest.mark.asyncio
async def test_a_slow_scraper_cannot_stall_the_collection(monkeypatch):
    """Whatever finished inside the budget is used; the rest keep their blurb."""
    monkeypatch.setattr(nar, "UPGRADE_BUDGET_S", 0.05)
    monkeypatch.setattr(nar, "UPGRADE_CONCURRENCY", 8)

    async def _scrape(url):
        await asyncio.sleep(5)
        return "w" * 3000

    arts = [_article(f"https://x.test/{i}", "b" * 300) for i in range(8)]
    with _patch_scraper(_scrape):
        bodies = await asyncio.wait_for(nar._upgrade_bodies(arts), timeout=2.0)

    assert bodies == {}


@pytest.mark.asyncio
async def test_one_failing_url_does_not_lose_the_others():
    async def _scrape(url):
        if url.endswith("bad"):
            raise RuntimeError("boom")
        return "w" * 3000

    arts = [_article("https://x.test/bad", "b" * 300),
            _article("https://x.test/good", "b" * 300)]
    with _patch_scraper(_scrape):
        bodies = await nar._upgrade_bodies(arts)

    assert "https://x.test/good" in bodies
    assert "https://x.test/bad" not in bodies


@pytest.mark.asyncio
async def test_the_upgrade_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(nar, "UPGRADE_LIMIT", 0)
    scraper = AsyncMock(return_value="w" * 3000)
    with _patch_scraper(scraper):
        bodies = await nar._upgrade_bodies([_article("https://x.test/a", "b" * 300)])

    assert bodies == {}
    assert scraper.await_count == 0


def test_the_trigger_and_the_scrapers_gate_are_the_same_number():
    """Asking for a body the scraper will then refuse is pure waste — and the
    two drifting apart is exactly how the 150/900 band opened up."""
    from app.scraper.core.content_quality import MIN_ARTICLE_CHARS

    assert nar.MIN_BODY_CHARS == MIN_ARTICLE_CHARS
