import pytest
import json
from unittest.mock import MagicMock, patch
from app.cycle.orchestration.state_manager import PipelineStateDB
from app.services.logging.unified_logger import DbLoggingHandler

@pytest.mark.asyncio
async def test_db_error_logging_execution_errors(mock_db):
    """Verify that execution errors are logged to the Postgres execution_errors table."""
    # Reset mock execution history
    mock_db.execute.reset_mock()
    
    cycle_id = "test-resilience-cycle"
    phase = "analyzing"
    ticker = "NVDA"
    error_type = "TimeoutError"
    error_message = "Connection to vLLM failed after 3 attempts"
    stack_trace = "Traceback (most recent call last): ..."
    
    PipelineStateDB.log_execution_error(
        cycle_id=cycle_id,
        phase=phase,
        ticker=ticker,
        error_type=error_type,
        error_message=error_message,
        stack_trace=stack_trace
    )
    
    # Assert execute was called
    assert mock_db.execute.called
    
    # Extract executed query and params
    calls = mock_db.execute.call_args_list
    insert_call = None
    for call in calls:
        query = call[0][0]
        if "INSERT INTO execution_errors" in query:
            insert_call = call
            break
            
    assert insert_call is not None, "INSERT INTO execution_errors query was not executed."
    
    # Verify parameter passing
    params = insert_call[0][1]
    # params: [id, cycle_id, phase, ticker, error_type, error_message, stack_trace]
    assert params[1] == cycle_id
    assert params[2] == phase
    assert params[3] == ticker
    assert params[4] == error_type
    assert params[5] == error_message
    assert params[6] == stack_trace


@pytest.mark.asyncio
async def test_cycle_checkpoint_save_and_resume(mock_db):
    """Verify that cycle checkpointing saves state to Postgres and retrieves/parses it correctly."""
    # Reset mock execution history
    mock_db.execute.reset_mock()
    
    cycle_id = "resilience-checkpoint-cycle"
    completed_phases = ["health", "collection"]
    completed_tickers = {"collection": ["AAPL", "MSFT"]}
    cycle_config = {"V2_TICKER_CONCURRENCY": 2}
    original_started_at = "2026-06-07T12:00:00+00:00"
    
    # Save checkpoint
    PipelineStateDB.save_checkpoint(
        cycle_id=cycle_id,
        completed_phases=completed_phases,
        completed_tickers=completed_tickers,
        cycle_config=cycle_config,
        original_started_at=original_started_at
    )
    
    # Check that INSERT INTO cycle_resume_state was called
    calls = mock_db.execute.call_args_list
    checkpoint_call = None
    for call in calls:
        query = call[0][0]
        if "INSERT INTO cycle_resume_state" in query:
            checkpoint_call = call
            break
            
    assert checkpoint_call is not None, "INSERT INTO cycle_resume_state was not executed"
    params = checkpoint_call[0][1]
    # params: [cycle_id, completed_phases_json, completed_tickers_json, cycle_config_json, original_started_at]
    assert params[0] == cycle_id
    assert json.loads(params[1]) == completed_phases
    assert json.loads(params[2]) == completed_tickers
    assert json.loads(params[3]) == cycle_config
    assert params[4] == original_started_at
    
    # Now simulate retrieval
    # Row sequence: cycle_id, status, completed_phases, completed_tickers, cycle_config, checkpoint_ts, original_started_at
    mock_row = (
        cycle_id,
        "interrupted",
        json.dumps(completed_phases),
        json.dumps(completed_tickers),
        json.dumps(cycle_config),
        "2026-06-07 13:00:00+00",
        original_started_at
    )
    
    # Setup mock cursor fetch result
    mock_db.fetchone.return_value = mock_row
    
    checkpoint = PipelineStateDB.get_checkpoint(cycle_id)
    
    assert checkpoint is not None
    assert checkpoint["cycle_id"] == cycle_id
    assert checkpoint["status"] == "interrupted"
    assert checkpoint["completed_phases"] == completed_phases
    assert checkpoint["completed_tickers"] == completed_tickers
    assert checkpoint["cycle_config"] == cycle_config
    assert checkpoint["original_started_at"] is not None
