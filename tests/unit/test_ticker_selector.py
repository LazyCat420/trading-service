from unittest.mock import MagicMock, patch
from contextlib import contextmanager
import pytest
from app.pipeline.ticker_selector import TickerSelector, TickerSelectionResult


def test_select_tickers_for_cycle_v2(monkeypatch, mock_db):
    """Verify ticker selection merges positions, watchlist, and discoveries."""

    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "information_schema" in query:
            mock_cursor.fetchone.return_value = (1,)
        elif "position_lots" in query:
            mock_cursor.fetchall.return_value = [("AAPL",), ("MSFT",)]
        elif "watchlist" in query:
            mock_cursor.fetchall.return_value = [("GOOG",)]
        elif "decision_outcomes WHERE created_at" in query:
            mock_cursor.fetchall.return_value = []
        elif "analysis_results WHERE created_at" in query:
            mock_cursor.fetchall.return_value = []
        elif "discovered_tickers" in query:
            assert "?" not in query, "SQLite placeholder '?' found! Must use '%s' for Postgres."
            mock_cursor.fetchall.return_value = [("TSLA", 100, "large", True, "2000-01-01")]
        else:
            mock_cursor.fetchall.return_value = []
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute

    @contextmanager
    def fake_get_db():
        yield mock_db

    # Patch at the root level so all modules get the fake DB
    monkeypatch.setattr("app.db.connection.get_db", fake_get_db)
    monkeypatch.setattr("app.pipeline.ticker_selector.get_db", fake_get_db)
    
    # Mock bot manager to avoid nested db calls
    monkeypatch.setattr("app.services.bot_manager.get_active_bot_id", lambda: "test-bot")

    res = TickerSelector.select_tickers_for_cycle_v2(["NVDA"], cap=50)

    assert "AAPL" in res.position_tickers
    assert "MSFT" in res.position_tickers
    assert "GOOG" in res.non_position_tickers
    assert "NVDA" in res.non_position_tickers
    assert "TSLA" in res.non_position_tickers

    assert len(res.position_tickers) == 2
    assert len(res.non_position_tickers) == 3

    assert len(res.all_tickers) == 5

@patch("app.pipeline.ticker_selector.get_db")
@patch("app.services.bot_manager.get_active_bot_id")
def test_selector_quarantine_filtering(mock_get_active_bot_id, mock_get_db):
    """Verify that the discovery query explicitly filters out quarantined tickers."""
    mock_get_active_bot_id.return_value = "test-bot"
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    executed_queries = []
    
    def side_effect_execute(query, params=None):
        executed_queries.append(query)
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute
    
    # We pass empty requested to force discovery logic to run
    TickerSelector.select_tickers_for_cycle_v2([], cap=10)
    
    discovery_query = next((q for q in executed_queries if "discovered_tickers" in q), None)
    assert discovery_query is not None
    assert "d.validation_status != 'quarantine'" in discovery_query

@patch("app.pipeline.ticker_selector.get_db")
@patch("app.services.bot_manager.get_active_bot_id")
def test_select_tickers_with_cooldown(mock_get_active_bot_id, mock_get_db):
    mock_get_active_bot_id.return_value = "test-bot"
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "information_schema" in query:
            mock_cursor.fetchone.return_value = (1,)
        elif "position_lots" in query:
            mock_cursor.fetchall.return_value = [("AAPL",)]
        elif "decision_outcomes WHERE created_at" in query:
            # AAPL is a position, should still be included despite cooldown
            mock_cursor.fetchall.return_value = [("AAPL",)] 
        elif "analysis_results WHERE created_at" in query:
            # NVDA requested, GOOG watchlist, both should be skipped
            mock_cursor.fetchall.return_value = [("NVDA",), ("GOOG",)] 
        elif "watchlist" in query:
            mock_cursor.fetchall.return_value = [("GOOG",), ("MSFT",)]
        elif "discovered_tickers" in query:
            mock_cursor.fetchall.return_value = [("TSLA", 100, "large", True, "2000-01-01")]
        else:
            mock_cursor.fetchall.return_value = []
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute
    
    res = TickerSelector.select_tickers_for_cycle_v2(["NVDA", "AMZN"], cap=50)
    
    assert "AAPL" in res.position_tickers # Position included regardless of cooldown
    assert "NVDA" in res.non_position_tickers # Requested, bypasses cooldown now
    assert "GOOG" not in res.non_position_tickers # Watchlist, skipped due to cooldown
    assert "AMZN" in res.non_position_tickers # Requested, not in cooldown
    assert "MSFT" in res.non_position_tickers # Watchlist, not in cooldown
    assert "TSLA" in res.non_position_tickers # Discovered, not in cooldown


@patch("app.pipeline.ticker_selector.get_db")
@patch("app.services.bot_manager.get_active_bot_id")
def test_select_tickers_completed_cycle_cooldown(mock_get_active_bot_id, mock_get_db):
    """Verify that tickers from the last COMPLETED cycle (status='done') are placed on cooldown."""
    mock_get_active_bot_id.return_value = "test-bot"
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "information_schema" in query:
            mock_cursor.fetchone.return_value = (1,)
        elif "position_lots" in query:
            mock_cursor.fetchall.return_value = [("AAPL",)]
        elif "decision_outcomes WHERE created_at" in query:
            mock_cursor.fetchall.return_value = []
        elif "analysis_results WHERE created_at" in query:
            mock_cursor.fetchall.return_value = []
        elif "cycle_benchmarks WHERE status = 'done'" in query:
            mock_cursor.fetchone.return_value = ("completed_cycle_123",)
        elif "cycle_ticker_benchmarks WHERE cycle_id" in query:
            # Tickers in last completed cycle: AAPL (position), MSFT (watchlist)
            mock_cursor.fetchall.return_value = [("AAPL",), ("MSFT",)]
        elif "watchlist" in query:
            mock_cursor.fetchall.return_value = [("MSFT",), ("GOOG",)]
        elif "discovered_tickers" in query:
            mock_cursor.fetchall.return_value = [("TSLA", 100, "large", True, "2000-01-01")]
        else:
            mock_cursor.fetchall.return_value = []
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute
    
    res = TickerSelector.select_tickers_for_cycle_v2([], cap=50)
    
    assert "AAPL" in res.position_tickers       # Position included regardless of cooldown
    assert "MSFT" not in res.non_position_tickers  # Watchlist, skipped due to completed cycle cooldown
    assert "GOOG" in res.non_position_tickers      # Watchlist, not in cooldown
    assert "TSLA" in res.non_position_tickers      # Discovered, not in cooldown


@patch("app.pipeline.ticker_selector.get_db")
@patch("app.services.bot_manager.get_active_bot_id")
def test_select_tickers_failed_cycle_no_cooldown(mock_get_active_bot_id, mock_get_db):
    """Verify that tickers from a FAILED/incomplete cycle are NOT placed on cooldown."""
    mock_get_active_bot_id.return_value = "test-bot"
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "information_schema" in query:
            mock_cursor.fetchone.return_value = (1,)
        elif "position_lots" in query:
            mock_cursor.fetchall.return_value = []
        elif "decision_outcomes WHERE created_at" in query:
            mock_cursor.fetchall.return_value = []
        elif "analysis_results WHERE created_at" in query:
            mock_cursor.fetchall.return_value = []
        elif "cycle_benchmarks WHERE status = 'done'" in query:
            # No completed cycle found
            mock_cursor.fetchone.return_value = None
        elif "watchlist" in query:
            mock_cursor.fetchall.return_value = [("MSFT",)]
        elif "discovered_tickers" in query:
            mock_cursor.fetchall.return_value = []
        else:
            mock_cursor.fetchall.return_value = []
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute
    
    res = TickerSelector.select_tickers_for_cycle_v2([], cap=50)
    
    assert "MSFT" in res.non_position_tickers  # Included since there was no completed cycle to put it on cooldown


@patch("app.pipeline.ticker_selector.get_db")
@patch("app.services.bot_manager.get_active_bot_id")
def test_ticker_selector_custom_caps_and_discovered_limit(mock_get_active_bot_id, mock_get_db):
    """Verify cap and discovered_tickers limits are strictly enforced."""
    mock_get_active_bot_id.return_value = "test-bot"
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db

    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "information_schema" in query:
            mock_cursor.fetchone.return_value = (1,)
        elif "position_lots" in query:
            # 1 Position ticker
            mock_cursor.fetchall.return_value = [("AAPL",)]
        elif "watchlist" in query:
            # 1 Watchlist ticker
            mock_cursor.fetchall.return_value = [("MSFT",)]
        elif "discovered_tickers" in query:
            # 5 Discovered tickers
            mock_cursor.fetchall.return_value = [
                ("TSLA", 100, "large", True, "2000-01-01"),
                ("NVDA", 90, "large", True, "2000-01-01"),
                ("AMZN", 80, "large", True, "2000-01-01"),
                ("GOOG", 70, "large", True, "2000-01-01"),
                ("META", 60, "large", True, "2000-01-01"),
            ]
        else:
            mock_cursor.fetchall.return_value = []
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute

    # Cap is 5, discovered_tickers budget is 2.
    # Expected: AAPL (position, 1), MSFT (watchlist, 1). Remaining non-position slots = 3.
    # But discovery budget is capped at 2.
    # Final non-position: MSFT (1) + 2 discoveries = 3.
    # Total selected = 4 (which is <= cap of 5).
    res = TickerSelector.select_tickers_for_cycle_v2([], cap=5, discovered_tickers=2)

    assert "AAPL" in res.position_tickers
    assert "MSFT" in res.non_position_tickers
    assert len(res.position_tickers) == 1
    assert len(res.non_position_tickers) == 3 # MSFT + 2 discoveries
    assert len(res.all_tickers) == 4


@patch("app.pipeline.ticker_selector.get_db")
@patch("app.services.bot_manager.get_active_bot_id")
def test_ticker_selector_small_cap_tuning_and_multi_cycle_cooldown(mock_get_active_bot_id, mock_get_db):
    """Verify that when cap < 10, the selector queries the last 5 completed cycles instead of 1."""
    mock_get_active_bot_id.return_value = "test-bot"
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db

    executed_queries = []

    def side_effect_execute(query, params=None):
        executed_queries.append((query, params))
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.fetchone.return_value = None
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute

    # Scenario A: cap >= 10 (e.g. 12) -> should query last 1 completed cycle
    TickerSelector.select_tickers_for_cycle_v2([], cap=12)
    
    # Scenario B: cap < 10 (e.g. 5) -> should query last 5 completed cycles
    TickerSelector.select_tickers_for_cycle_v2([], cap=5)

    # Find the cycle benchmarks queries and their LIMIT parameter / value
    limit_queries = [q for q, p in executed_queries if "cycle_benchmarks" in q]
    params_passed = [p for q, p in executed_queries if "cycle_benchmarks" in q]

    assert len(limit_queries) == 3
    # Verify first query (cap >= 10) passes LIMIT 1
    assert params_passed[0] == [1]
    # Verify second query (cap < 10) passes LIMIT 5
    assert params_passed[1] == [5]
    # Verify third query (cap < 10 overlap logging check) has LIMIT 1
    assert "LIMIT 1" in limit_queries[2]


@patch("app.pipeline.ticker_selector.get_db")
@patch("app.services.bot_manager.get_active_bot_id")
def test_ticker_selector_ban_filtering(mock_get_active_bot_id, mock_get_db):
    """Verify that tickers in ticker_bans are filtered out by TickerSelector."""
    mock_get_active_bot_id.return_value = "test-bot"
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "information_schema" in query:
            mock_cursor.fetchone.return_value = (1,)
        elif "position_lots" in query:
            mock_cursor.fetchall.return_value = []
        elif "watchlist" in query:
            mock_cursor.fetchall.return_value = [("GOOG",), ("TSLA",)]
        elif "ticker_bans" in query:
            mock_cursor.fetchall.return_value = [("TSLA",)]
        elif "discovered_tickers" in query:
            mock_cursor.fetchall.return_value = [("NVDA", 100, "large", True, "2000-01-01")]
        else:
            mock_cursor.fetchall.return_value = []
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute
    
    res = TickerSelector.select_tickers_for_cycle_v2([], cap=50)
    
    assert "GOOG" in res.non_position_tickers
    assert "NVDA" in res.non_position_tickers
    assert "TSLA" not in res.non_position_tickers # TSLA is banned, should be filtered out!


@patch("app.pipeline.ticker_selector.get_db")
@patch("app.services.bot_manager.get_active_bot_id")
def test_ticker_selector_format_filtering(mock_get_active_bot_id, mock_get_db):
    """Verify that tickers with numbers/dots (e.g. 003160.KS) are filtered out, but hyphens (e.g. BRK-B) are allowed."""
    mock_get_active_bot_id.return_value = "test-bot"
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "information_schema" in query:
            mock_cursor.fetchone.return_value = (1,)
        elif "position_lots" in query:
            mock_cursor.fetchall.return_value = []
        elif "watchlist" in query:
            mock_cursor.fetchall.return_value = []
        elif "discovered_tickers" in query:
            # Return BRK-B (valid), 003160.KS (invalid dot/numbers), 0700 (invalid numbers)
            mock_cursor.fetchall.return_value = [
                ("BRK-B", 100, "large", True, "2000-01-01"),
                ("003160.KS", 90, "large", True, "2000-01-01"),
                ("0700", 80, "large", True, "2000-01-01"),
            ]
        else:
            mock_cursor.fetchall.return_value = []
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute
    
    res = TickerSelector.select_tickers_for_cycle_v2([], cap=50)
    
    assert "BRK-B" in res.non_position_tickers
    assert "003160.KS" not in res.non_position_tickers
    assert "0700" not in res.non_position_tickers


