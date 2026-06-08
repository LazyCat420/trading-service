import pytest
import json
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

from app.cycle.context import CycleContext
from app.agents.planner_agent import run_ticker_curator
from app.cycle.orchestration.orchestrator_core import OrchestratorCoreMixin

@pytest.fixture
def mock_db_with_news(mock_db):
    def side_effect(query, params=None):
        cursor = MagicMock()
        cursor.execute.return_value = cursor
        cursor.fetchone.return_value = None
        if "news_articles" in query:
            cursor.fetchall.return_value = [
                ("Tesla hits record high", "Reuters", None, "Tesla records high growth and deliveries"),
                ("Apple launches new Vision Pro", "Bloomberg", None, "Apple releases next gen headset"),
            ]
        else:
            cursor.fetchall.return_value = []
        return cursor
        
    mock_db.execute.side_effect = side_effect
    return mock_db

@pytest.fixture
def mock_run_agent_curator():
    with patch("app.agents.base_agent.run_agent") as mock_run:
        mock_run.return_value = {
            "response": '{"selected_tickers": ["TSLA"], "justification": {"TSLA": "Tesla news is hot"}, "skipped_tickers": {"AAPL": "Apple news is quiet"}}',
            "agent": "curator",
            "ticker": "global"
        }
        yield mock_run

@pytest.mark.asyncio
async def test_run_ticker_curator(mock_db_with_news, mock_run_agent_curator, monkeypatch):
    @contextmanager
    def fake_get_db():
        yield mock_db_with_news

    monkeypatch.setattr("app.db.connection.get_db", fake_get_db)

    result = await run_ticker_curator(
        candidates=["TSLA", "AAPL"],
        position_tickers=["AAPL"],
        cycle_id="cycle-123",
        bot_id="bot-456"
    )

    assert result["agent"] == "curator"
    assert "TSLA" in result["response"]
    assert mock_run_agent_curator.called
    system_prompt = mock_run_agent_curator.call_args.kwargs["system_prompt"]
    user_prompt = mock_run_agent_curator.call_args.kwargs["user_prompt"]
    assert "Portfolio Curator" in system_prompt
    assert "TSLA" in user_prompt
    assert "AAPL [ACTIVE PORTFOLIO POSITION]" in user_prompt

@pytest.mark.asyncio
async def test_decide_tickers_to_process_success(mock_db_with_news, mock_run_agent_curator, monkeypatch):
    @contextmanager
    def fake_get_db():
        yield mock_db_with_news

    monkeypatch.setattr("app.db.connection.get_db", fake_get_db)

    ctx = CycleContext(
        tickers=["TSLA", "AAPL"],
        collect=True,
        analyze=True,
        trade=True,
        cycle_id="cycle-123",
        bot_id="bot-456",
        max_tickers=0,
        dynamic_selection_mode=True
    )

    class DummyOrchestrator(OrchestratorCoreMixin):
        _state = {"position_tickers": ["AAPL"]}

    selected = await DummyOrchestrator.decide_tickers_to_process(ctx, "bot-456")
    assert selected == ["TSLA"]

@pytest.mark.asyncio
async def test_decide_tickers_to_process_empty_fallback(mock_db_with_news, monkeypatch):
    @contextmanager
    def fake_get_db():
        yield mock_db_with_news

    monkeypatch.setattr("app.db.connection.get_db", fake_get_db)

    with patch("app.agents.base_agent.run_agent") as mock_run:
        mock_run.return_value = {
            "response": "",  # Empty response to trigger failure
            "agent": "curator",
            "ticker": "global"
        }

        ctx = CycleContext(
            tickers=["TSLA", "AAPL"],
            collect=True,
            analyze=True,
            trade=True,
            cycle_id="cycle-123",
            bot_id="bot-456",
            max_tickers=0,
            dynamic_selection_mode=True
        )

        class DummyOrchestrator(OrchestratorCoreMixin):
            _state = {"position_tickers": ["AAPL"]}

        with pytest.raises(ValueError, match="Curator agent returned empty response"):
            await DummyOrchestrator.decide_tickers_to_process(ctx, "bot-456")
