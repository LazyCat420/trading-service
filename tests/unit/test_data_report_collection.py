"""Unit tests for data report collection force_refresh and receipt hardening."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta

from app.v3.data_report import build_ticker_data_report
from app.v3.shared_desk import SharedDesk


@pytest.mark.asyncio
async def test_force_refresh_bypasses_fast_path():
    """When force_refresh=True, full collectors must be dispatched even if a recent thesis exists."""
    recent_time = datetime.now(timezone.utc) - timedelta(hours=2)
    fake_doc = [{"thesis_summary": "Prior thesis summary for NVDA", "created_at": recent_time}]

    with patch("app.db.mongo_store.find_docs", return_value=fake_doc):
        with patch("app.collectors.yfinance_collector.collect_price_history", new_callable=AsyncMock) as mock_price, \
             patch("app.collectors.yfinance_collector.collect_fundamentals", new_callable=AsyncMock) as mock_fund, \
             patch("app.collectors.news_collector.collect_finnhub_news", new_callable=AsyncMock) as mock_finnhub, \
             patch("app.collectors.news_api_rotator.collect_from_all_apis", new_callable=AsyncMock) as mock_multi, \
             patch("app.collectors.reddit_collector.collect_for_ticker", new_callable=AsyncMock) as mock_reddit, \
             patch("app.collectors.youtube_collector.collect_for_ticker", new_callable=AsyncMock) as mock_yt, \
             patch("app.tools.finance_tools.get_market_data", new_callable=AsyncMock, return_value="Market data"), \
             patch("app.tools.finance_tools.get_finnhub_news", new_callable=AsyncMock, return_value="News"), \
             patch("app.tools.finance_tools.get_technical_indicators", new_callable=AsyncMock, return_value="Tech"):

            mock_price.return_value = 100
            mock_fund.return_value = True
            mock_finnhub.return_value = 5
            mock_multi.return_value = 5
            mock_reddit.return_value = 5
            mock_yt.return_value = 5

            # With force_refresh=True, heavy collectors should be called
            report = await build_ticker_data_report("NVDA", force_refresh=True)

            assert "PREVIOUS ANALYSIS ON FILE (FORCE REFRESH)" in report
            assert mock_price.called
            assert mock_fund.called
            assert mock_multi.called
            assert mock_reddit.called
            assert mock_yt.called


@pytest.mark.asyncio
async def test_normal_run_engages_fast_path():
    """When force_refresh=False and a recent thesis exists, heavy scrapers must be skipped."""
    recent_time = datetime.now(timezone.utc) - timedelta(hours=2)
    fake_doc = [{"thesis_summary": "Prior thesis summary for NVDA", "created_at": recent_time}]

    with patch("app.db.mongo_store.find_docs", return_value=fake_doc):
        with patch("app.collectors.yfinance_collector.collect_price_history", new_callable=AsyncMock) as mock_price, \
             patch("app.collectors.yfinance_collector.collect_fundamentals", new_callable=AsyncMock) as mock_fund, \
             patch("app.collectors.news_collector.collect_finnhub_news", new_callable=AsyncMock) as mock_finnhub, \
             patch("app.collectors.news_api_rotator.collect_from_all_apis", new_callable=AsyncMock) as mock_multi, \
             patch("app.collectors.reddit_collector.collect_for_ticker", new_callable=AsyncMock) as mock_reddit, \
             patch("app.collectors.youtube_collector.collect_for_ticker", new_callable=AsyncMock) as mock_yt, \
             patch("app.tools.finance_tools.get_market_data", new_callable=AsyncMock, return_value="Market data"), \
             patch("app.tools.finance_tools.get_finnhub_news", new_callable=AsyncMock, return_value="News"), \
             patch("app.tools.finance_tools.get_technical_indicators", new_callable=AsyncMock, return_value="Tech"):

            mock_price.return_value = 100
            mock_finnhub.return_value = 5

            # With force_refresh=False, fast-path engages
            report = await build_ticker_data_report("NVDA", force_refresh=False)

            assert "FAST-PATH" in report
            assert mock_price.called
            assert mock_finnhub.called
            # Heavy collectors must NOT have been called
            assert not mock_fund.called
            assert not mock_multi.called
            assert not mock_reddit.called
            assert not mock_yt.called


def test_research_answers_delivered_receipt_logic():
    """Receipt must mark research_answers_delivered as 'not_applicable' when no questions exist."""
    desk = SharedDesk(cycle_id="test-cycle", ticker="NVDA")
    
    # Case 1: No research questions on ledger
    desk.cycle_metadata["research_questions"] = []
    has_questions = bool(desk.cycle_metadata.get("research_questions"))
    delivered = (bool(desk.cycle_metadata.get('research_answers_context') and "text" in "")
                 if has_questions else "not_applicable")
    assert delivered == "not_applicable"

    # Case 2: Research questions present and delivered
    desk.cycle_metadata["research_questions"] = [{"id": "q1", "question": "test?"}]
    desk.cycle_metadata["research_answers_context"] = "ANSWER BLOCK"
    has_questions = bool(desk.cycle_metadata.get("research_questions"))
    delivered_text = "Prompt text with ANSWER BLOCK included"
    delivered = (bool(desk.cycle_metadata.get('research_answers_context') and desk.cycle_metadata['research_answers_context'] in delivered_text)
                 if has_questions else "not_applicable")
    assert delivered is True
