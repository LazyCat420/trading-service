"""Riders on f881a63 (see docs/NEWS_STREAMING_AUDIT_2026-08-31.md):
fallback-rate visibility and no silent freshness fabrication."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from app.collectors.news_collector import _scrape_with_timeout, collect_feed


def test_scrape_fallback_is_counted():
    async def boom(url):
        raise RuntimeError("scraper down")

    stats = {}
    with patch("app.collectors.news_collector._scrape_article_body_via_service", boom):
        out = asyncio.run(_scrape_with_timeout("http://x", "FALLBACK", timeout=0.5, stats=stats))
    assert out == "FALLBACK"
    assert stats.get("fallback") == 1 and not stats.get("scraped")


def test_scrape_success_is_counted():
    async def ok(url):
        return "B" * 200

    stats = {}
    with patch("app.collectors.news_collector._scrape_article_body_via_service", ok):
        out = asyncio.run(_scrape_with_timeout("http://x", "FALLBACK", timeout=0.5, stats=stats))
    assert out == "B" * 200
    assert stats.get("scraped") == 1 and not stats.get("fallback")


@pytest.mark.asyncio
async def test_unparsable_published_at_is_marked_estimated():
    mock_summary = (
        "Nvidia (NVDA) reported a massive surge in artificial intelligence datacenter demand, "
        "expanding its next-generation Blackwell GPU production across enterprise markets, "
        "solidifying strong revenue outlook and market leadership."
    )
    mock_items = [{
        "title": "Nvidia expands AI datacenter market with Blackwell architecture",
        "url": "https://example.com/nvda",
        "summary": mock_summary,
        "publisher": "Reuters",
        "published_at": "not-a-real-date",
    }]

    with patch("app.services.scraper_client.scraper_client.collect", new=AsyncMock(return_value=mock_items)), \
         patch("app.processors.ticker_extractor.get_ticker_symbols", new=AsyncMock(return_value=["NVDA"])), \
         patch("app.processors.dedup_engine.mongo_store.count_docs", return_value=0), \
         patch("app.processors.dedup_engine.DedupEngine._get_recent_items", return_value=[]), \
         patch("app.collectors.news_collector._scrape_with_timeout", new=AsyncMock(return_value=mock_summary)), \
         patch("app.db.mongo_store.upsert_doc") as mock_upsert:

        count = await collect_feed("Reuters", "https://reuters.com/rss", emit_cb=None)
        assert count == 1
        doc = mock_upsert.call_args.args[2]
        assert doc["published_at_estimated"] is True, "unparsable date must be MARKED, not silently now()"


@pytest.mark.asyncio
async def test_parsable_published_at_is_not_marked():
    mock_summary = (
        "Nvidia (NVDA) reported a massive surge in artificial intelligence datacenter demand, "
        "expanding its next-generation Blackwell GPU production across enterprise markets, "
        "solidifying strong revenue outlook and market leadership."
    )
    mock_items = [{
        "title": "Nvidia keeps its datacenter lead with Blackwell",
        "url": "https://example.com/nvda2",
        "summary": mock_summary,
        "publisher": "Reuters",
        "published_at": "2026-08-31T10:00:00Z",
    }]

    with patch("app.services.scraper_client.scraper_client.collect", new=AsyncMock(return_value=mock_items)), \
         patch("app.processors.ticker_extractor.get_ticker_symbols", new=AsyncMock(return_value=["NVDA"])), \
         patch("app.processors.dedup_engine.mongo_store.count_docs", return_value=0), \
         patch("app.processors.dedup_engine.DedupEngine._get_recent_items", return_value=[]), \
         patch("app.collectors.news_collector._scrape_with_timeout", new=AsyncMock(return_value=mock_summary)), \
         patch("app.db.mongo_store.upsert_doc") as mock_upsert:

        count = await collect_feed("Reuters", "https://reuters.com/rss", emit_cb=None)
        assert count == 1
        doc = mock_upsert.call_args.args[2]
        assert doc["published_at_estimated"] is False
