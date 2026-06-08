import pytest
import json
from unittest.mock import MagicMock
from contextlib import contextmanager
from app.tools.schedule_tools import create_or_update_schedule


@pytest.mark.asyncio
async def test_create_schedule_with_custom_parameters(monkeypatch, mock_db):
    """Verify create path saves custom tickers, max_tickers, and discovered_tickers."""
    @contextmanager
    def fake_get_db():
        yield mock_db

    monkeypatch.setattr("app.tools.schedule_tools.get_db", fake_get_db)

    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "COUNT(*)" in query:
            mock_cursor.fetchone.return_value = (2,)
        else:
            mock_cursor.fetchone.return_value = None
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute

    # Call tool to CREATE a new schedule
    result_str = await create_or_update_schedule(
        name="Test Follow-up Cycle",
        interval_hours=2.0,
        tickers=["AAPL", "TSLA"],
        max_tickers=10,
        discovered_tickers=5,
    )
    result = json.loads(result_str)

    assert result["status"] == "created"
    
    # Locate the INSERT query in executed calls
    insert_call = None
    for call in mock_db.execute.call_args_list:
        query = call[0][0]
        if "INSERT INTO cycle_schedules" in query:
            insert_call = call
            break

    assert insert_call is not None
    params = insert_call[0][1]
    
    # Columns in insert list:
    # id, name, schedule_type, cron_expression, interval_hours, collect, analyze, trade, tickers, max_tickers, discovered_tickers, market_hours_only, is_active, created_at, updated_at
    # tickers (index 8) -> json.dumps(["AAPL", "TSLA"])
    # max_tickers (index 9) -> 10
    # discovered_tickers (index 10) -> 5
    assert json.loads(params[8]) == ["AAPL", "TSLA"]
    assert params[9] == 10
    assert params[10] == 5


@pytest.mark.asyncio
async def test_update_schedule_merging_provided_values(monkeypatch, mock_db):
    """Verify update path merges new values if passed, and keeps existing values if None."""
    @contextmanager
    def fake_get_db():
        yield mock_db

    monkeypatch.setattr("app.tools.schedule_tools.get_db", fake_get_db)

    def side_effect_execute(query, params=None):
        mock_cursor = MagicMock()
        if "SELECT tickers" in query:
            mock_cursor.fetchone.return_value = (
                '["AAPL"]', # existing tickers
                20,         # existing max_tickers
                10,         # existing discovered_tickers
            )
        else:
            mock_cursor.fetchone.return_value = None
        return mock_cursor

    mock_db.execute.side_effect = side_effect_execute

    # Call tool to UPDATE with ONLY max_tickers changed
    result_str = await create_or_update_schedule(
        name="Auto-Recovery Schedule",
        update_schedule_id="sch-default",
        max_tickers=5, # new value
        tickers=None,   # should keep existing
        discovered_tickers=None, # should keep existing
    )
    result = json.loads(result_str)

    assert result["status"] == "updated"

    # Locate the UPDATE query
    update_call = None
    for call in mock_db.execute.call_args_list:
        query = call[0][0]
        if "UPDATE cycle_schedules" in query:
            update_call = call
            break

    assert update_call is not None
    params = update_call[0][1]
    
    # UPDATE parameters:
    # name, schedule_type, cron_expression, interval_hours, collect, analyze, market_hours_only, tickers, max_tickers, discovered_tickers, updated_at, id
    # tickers (index 7) -> keep existing '["AAPL"]'
    # max_tickers (index 8) -> new value 5
    # discovered_tickers (index 9) -> keep existing 10
    assert params[7] == '["AAPL"]'
    assert params[8] == 5
    assert params[9] == 10
