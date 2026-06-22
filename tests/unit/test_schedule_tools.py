import pytest
import json
from unittest.mock import MagicMock
from contextlib import contextmanager
from app.tools.schedule_tools import create_or_update_schedule


@pytest.mark.asyncio
async def test_create_schedule_with_custom_parameters(monkeypatch, mock_db):
    """Verify create path saves policy-driven scheduling parameters."""
    @contextmanager
    def fake_get_db():
        yield mock_db

    monkeypatch.setattr("app.tools.schedule_tools.get_db", fake_get_db)
    
    # Mock validator
    mock_validator = MagicMock()
    mock_validator.validate_proposal.return_value = (True, "")
    monkeypatch.setattr("app.validation.schedule_validator.ScheduleValidator", mock_validator)

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
        schedule_scope="portfolio",
        review_intent="monitor",
        urgency="medium",
        earliest_window="next_pre_market",
        anti_overtrading_justification="Regular check",
        interval_hours=2.0,
        tickers=["AAPL", "TSLA"]
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
    # id(0), name(1), schedule_type(2), cron_expression(3), interval_hours(4), schedule_scope(5), review_intent(6), urgency(7), earliest_window(8), reason_codes(9), confidence(10), anti_overtrading_justification(11), tickers(12), max_tickers(13), discovered_tickers(14), is_active(15), created_at(16), updated_at(17)
    
    assert params[1] == "Test Follow-up Cycle"
    assert params[5] == "portfolio"
    assert params[6] == "monitor"
    assert params[7] == "medium"
    assert params[8] == "next_pre_market"
    assert json.loads(params[12]) == ["AAPL", "TSLA"]


@pytest.mark.asyncio
async def test_update_schedule_merging_provided_values(monkeypatch, mock_db):
    """Verify update path merges new values if passed, and keeps existing values if None."""
    @contextmanager
    def fake_get_db():
        yield mock_db

    monkeypatch.setattr("app.tools.schedule_tools.get_db", fake_get_db)
    
    # Mock validator
    mock_validator = MagicMock()
    mock_validator.validate_proposal.return_value = (True, "")
    monkeypatch.setattr("app.validation.schedule_validator.ScheduleValidator", mock_validator)

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

    # Call tool to UPDATE
    result_str = await create_or_update_schedule(
        name="Auto-Recovery Schedule",
        schedule_scope="single_ticker",
        review_intent="reassess",
        urgency="high",
        earliest_window="midday",
        anti_overtrading_justification="News catalyst",
        update_schedule_id="sch-default",
        tickers=None,   # should keep existing
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
    # name(0), schedule_type(1), cron_expression(2), interval_hours(3), schedule_scope(4), review_intent(5), urgency(6), earliest_window(7), reason_codes(8), confidence(9), anti_overtrading_justification(10), tickers(11), max_tickers(12), discovered_tickers(13), updated_at(14), id(15)
    assert params[4] == "single_ticker"
    assert params[11] == '["AAPL"]'
