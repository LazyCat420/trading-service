import pytest
import uuid
import sys
from unittest.mock import patch, MagicMock

# Ensure app is importable
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.autoresearch.eval_engine import (
    TraceRecord,
    evaluate_trace,
    classify_failure,
    process_and_store_trace,
    evaluate_confidence_calibration
)

@pytest.fixture
def mock_db():
    with patch("app.autoresearch.eval_engine.get_db") as mock_get_db:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.execute.return_value = mock_cursor
        mock_get_db.return_value = mock_conn
        yield mock_conn

def test_evaluate_trace():
    """Test the trace scoring logic directly."""
    trace = TraceRecord(
        id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        tokens_before=1000,
        tokens_after=1500,
        stop_reason="completed",
        tool_result_summary="Found solid evidence."
    )
    score = evaluate_trace(trace)
    
    assert score["completion_score"] == 40.0
    assert score["tool_correctness_score"] == 25.0
    assert score["efficiency_score"] == 20.0  # Used 500 tokens, which is < 5000
    assert score["error_recovery_score"] == 10.0
    assert score["stop_quality_score"] == 5.0
    assert score["final_score"] == 100.0

def test_classify_failure():
    """Test failure classification."""
    trace = TraceRecord(
        id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        decision_action="HOLD",
        decision_confidence=75.0,
        pnl_pct=-5.0,
        stop_reason="completed"
    )
    # Give a score < 70 to trigger classification
    score = {"final_score": 65.0, "completion_score": 40.0}
    
    bucket = classify_failure(trace, score)
    assert bucket == "hold_bias"

def test_process_and_store_trace(mock_db):
    """Test the end-to-end evaluation and DB insertion process."""
    trace = TraceRecord(
        id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        tokens_before=1000,
        tokens_after=1500,
        stop_reason="completed"
    )
    
    process_and_store_trace(trace)
    assert mock_db.execute.call_count >= 1
    # Check that it inserted into eval_scores
    call_args = mock_db.execute.call_args_list[0][0]
    assert "INSERT INTO eval_scores" in call_args[0]

def test_evaluate_confidence_calibration(mock_db):
    """Smoke test for confidence calibration logic."""
    # Setup mock returns: confidence, outcome, pnl_pct
    mock_db.execute.return_value.fetchall.return_value = [
        (80.0, "WIN", 5.0),
        (60.0, "WIN", 2.0),
        (90.0, "LOSS", -10.0)
    ]
    
    result = evaluate_confidence_calibration(ticker="AAPL", limit=10)
    
    assert result["status"] == "ok"
    assert result["sample_count"] == 3
    assert result["win_count"] == 2
    assert result["loss_count"] == 1
    assert "calibration_score" in result
