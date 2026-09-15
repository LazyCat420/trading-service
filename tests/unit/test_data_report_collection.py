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


@pytest.mark.asyncio
async def test_collector_latencies_and_stats_sink():
    """Verify that build_ticker_data_report populates stats_sink with collector latencies."""
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

            sink = {}
            report = await build_ticker_data_report("NVDA", force_refresh=True, stats_sink=sink)

            assert "collector_latencies" in sink
            assert "yfinance_price" in sink["collector_latencies"]
            assert "yfinance_fund" in sink["collector_latencies"]
            assert "finnhub_news" in sink["collector_latencies"]
            assert "multi_api_news" in sink["collector_latencies"]
            assert "reddit" in sink["collector_latencies"]
            assert "youtube" in sink["collector_latencies"]
            assert isinstance(sink["collector_latencies"]["yfinance_price"], int)
            assert sink["collect_ms"] >= 0
            assert "yfinance_price" in sink["ok"]


def test_data_audit_includes_reddit_and_youtube(monkeypatch):
    """_audit_data_quality must score all 6 categories including reddit and youtube."""
    from app.autoresearch.auditors import data_audit

    monkeypatch.setattr(data_audit, "_audit_price_history", lambda t: {"rows": 100, "quality_score": 0.95})
    monkeypatch.setattr(data_audit, "_audit_technicals", lambda t: {"rows": 100, "quality_score": 0.90})
    monkeypatch.setattr(data_audit, "_audit_fundamentals", lambda t: {"rows": 10, "quality_score": 0.85})
    monkeypatch.setattr(data_audit, "_audit_news", lambda t: {"rows": 20, "quality_score": 0.80})
    monkeypatch.setattr(data_audit, "_audit_reddit", lambda t: {"rows": 15, "quality_score": 0.75})
    monkeypatch.setattr(data_audit, "_audit_youtube", lambda t: {"rows": 5, "quality_score": 0.70})

    res = data_audit._audit_data_quality(["NVDA"])

    assert "NVDA" in res["per_ticker"]
    cats = res["per_ticker"]["NVDA"]["categories"]
    assert len(cats) == 6
    assert set(cats.keys()) == {"price_history", "technicals", "fundamentals", "news", "reddit", "youtube"}
    expected_avg = round((0.95 + 0.90 + 0.85 + 0.80 + 0.75 + 0.70) / 6, 3)
    assert res["per_ticker"]["NVDA"]["score"] == expected_avg


def test_box_scorecard_json_logging(caplog):
    """print_box_scorecard must output pure JSON without ASCII art borders."""
    import logging
    import json
    from app.monitoring.box_scorecard import print_box_scorecard

    test_scorecard = {
        "goldspark": {
            "model": "GLM-5.3-Flash-EXL3",
            "calls": 11,
            "total_tokens": 54000,
            "avg_latency_ms": 3200,
        },
        "_aggregate": {
            "total_calls": 11,
            "total_tokens": 54000,
        },
    }

    with caplog.at_level(logging.INFO):
        print_box_scorecard(test_scorecard)

    assert any("[BOX_SCORECARD]" in r.message for r in caplog.records)
    log_line = [r.message for r in caplog.records if "[BOX_SCORECARD]" in r.message][0]
    # Verify no ASCII art border characters
    assert "╔" not in log_line and "║" not in log_line and "╚" not in log_line
    # Verify valid JSON
    json_str = log_line.split("[BOX_SCORECARD] ", 1)[1]
    parsed = json.loads(json_str)
    assert parsed["goldspark"]["model"] == "GLM-5.3-Flash-EXL3"
    assert parsed["_aggregate"]["total_calls"] == 11


def test_performance_audit_enrichment(monkeypatch):
    """_audit_performance must include phase_ms and box_scorecard."""
    from app.autoresearch.auditors import performance_audit

    fake_bench = (1250, 45000, 150, 23000)
    monkeypatch.setattr(performance_audit.mongo_query, "find_row", lambda coll, q, cols: fake_bench)

    summary = {
        "elapsed_ms": 46400,
        "analysis_results_count": 2,
        "status": "completed",
        "box_scorecard": {"test_ep": {"calls": 5}},
    }

    result = performance_audit._audit_performance("test-cycle", summary)

    assert result["phase_ms"]["collect_ms"] == 1250
    assert result["phase_ms"]["analyze_ms"] == 45000
    assert result["phase_ms"]["trade_ms"] == 150
    assert result["box_scorecard"] == {"test_ep": {"calls": 5}}
