"""
Unit test for real-time news streaming and granular feed progress emission.
"""
import pytest
from unittest.mock import AsyncMock, patch
from app.collectors.news_collector import safe_emit, collect_feed


@pytest.mark.asyncio
async def test_safe_emit_passes_data():
    events = []

    def mock_emit(step, detail, status="ok", data=None):
        events.append({"step": step, "detail": detail, "status": status, "data": data or {}})

    safe_emit(
        mock_emit,
        "news_scraped",
        "📰 CNBC: 'Apple releases M5' -> ['AAPL']",
        status="ok",
        data={"kind": "news_article_scraped", "publisher": "CNBC", "tickers": ["AAPL"]},
    )

    assert len(events) == 1
    assert events[0]["step"] == "news_scraped"
    assert events[0]["data"]["kind"] == "news_article_scraped"
    assert events[0]["data"]["tickers"] == ["AAPL"]


@pytest.mark.asyncio
async def test_collect_feed_streaming_emit():
    mock_summary = (
        "Nvidia (NVDA) reported a massive surge in artificial intelligence datacenter demand, "
        "expanding its next-generation Blackwell GPU production across enterprise markets, "
        "solidifying strong revenue outlook and market leadership."
    )
    mock_items = [
        {
            "title": "Nvidia expands AI datacenter market with Blackwell architecture",
            "url": "https://example.com/nvda",
            "summary": mock_summary,
            "publisher": "Reuters",
            "published_at": "2026-08-31T10:00:00Z",
        }
    ]

    emitted_events = []

    def mock_emit(step, detail, status="ok", data=None):
        emitted_events.append({"step": step, "detail": detail, "status": status, "data": data or {}})

    with patch("app.services.scraper_client.scraper_client.collect", new=AsyncMock(return_value=mock_items)), \
         patch("app.processors.ticker_extractor.get_ticker_symbols", new=AsyncMock(return_value=["NVDA"])), \
         patch("app.processors.dedup_engine.mongo_store.count_docs", return_value=0), \
         patch("app.processors.dedup_engine.DedupEngine._get_recent_items", return_value=[]), \
         patch("app.collectors.news_collector._scrape_with_timeout", new=AsyncMock(return_value=mock_summary)), \
         patch("app.db.mongo_store.upsert_doc") as mock_upsert:

        count = await collect_feed("Reuters", "https://reuters.com/rss", emit_cb=mock_emit)
        assert count == 1
        assert mock_upsert.called

        scraped_events = [e for e in emitted_events if e["step"] == "news_scraped"]
        assert len(scraped_events) == 1
        assert scraped_events[0]["data"]["kind"] == "news_article_scraped"
        assert scraped_events[0]["data"]["publisher"] == "Reuters"
        assert "NVDA" in scraped_events[0]["data"]["tickers"]
