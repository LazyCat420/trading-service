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
    """Isolate eval_engine's store.

    Was `patch("app.autoresearch.eval_engine.get_db")`, a symbol the module no
    longer has — so this fixture raised AttributeError at SETUP and every test
    using it ERRORED. Errors are not failures, so the suite summary read
    "0 failed" while 16 tests never ran at all.

    Patches both halves: eval_engine writes through mongo_store
    (insert_docs / distinct_values) and reads through mongo_query.find_rows.
    """
    store, query = MagicMock(), MagicMock()
    store.distinct_values.return_value = []
    query.find_rows.return_value = []
    with patch("app.autoresearch.eval_engine.mongo_store", store), \
         patch("app.autoresearch.eval_engine.mongo_query", query):
        yield store

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

    # Was `"INSERT INTO eval_scores" in sql`. The write is a Mongo insert now,
    # so assert on the collection and the document — which also checks the
    # score actually reached the row, where the SQL substring only proved a
    # statement mentioning the table had been issued.
    assert mock_db.insert_docs.call_count >= 1
    collections = [c[0][0] for c in mock_db.insert_docs.call_args_list]
    assert "eval_scores" in collections
    doc = next(c[0][1][0] for c in mock_db.insert_docs.call_args_list
               if c[0][0] == "eval_scores")
    assert doc["run_id"] == trace.run_id
    assert isinstance(doc["final_score"], (int, float))

def test_evaluate_confidence_calibration(mock_db):
    """Smoke test for confidence calibration logic."""
    # decision_outcomes rows come back from mongo_query.find_rows as TUPLES
    # in the requested column order — ('confidence', 'outcome', 'pnl_pct').
    from unittest.mock import patch as _patch

    rows = [(80.0, "WIN", 5.0), (60.0, "WIN", 2.0), (90.0, "LOSS", -10.0)]
    query = MagicMock()
    query.find_rows.return_value = rows
    with _patch("app.autoresearch.eval_engine.mongo_query", query):
        result = evaluate_confidence_calibration(ticker="AAPL", limit=10)

    # The ticker must reach the filter: a calibration computed over the whole
    # book would answer the same for every symbol.
    assert query.find_rows.call_args[0][1]["ticker"] == "AAPL"
    
    assert result["status"] == "ok"
    assert result["sample_count"] == 3
    assert result["win_count"] == 2
    assert result["loss_count"] == 1
    assert "calibration_score" in result
