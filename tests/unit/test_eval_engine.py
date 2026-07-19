import pytest
from app.autoresearch.eval_engine import evaluate_trace, classify_failure, TraceRecord

def test_evaluate_trace_perfect():
    trace = TraceRecord(
        id="test-1",
        run_id="run-1",
        stop_reason="completed",
        tool_result_summary="Successfully fetched financial data.",
        tokens_before=100,
        tokens_after=200,
    )
    score = evaluate_trace(trace)
    assert score["completion_score"] == 40.0
    assert score["tool_correctness_score"] == 25.0
    assert score["efficiency_score"] == 20.0
    assert score["error_recovery_score"] == 10.0
    assert score["stop_quality_score"] == 5.0
    assert score["final_score"] == 100.0

def test_evaluate_trace_with_errors():
    """A failed tool call zeroes correctness but recovery credits the completed run."""
    trace = TraceRecord(
        id="test-2",
        run_id="run-2",
        stop_reason="completed",
        tool_result_summary="Error: ticker not found",
    )
    score = evaluate_trace(trace)
    assert score["completion_score"] == 40.0
    assert score["tool_correctness_score"] == 0.0
    assert score["error_recovery_score"] == 10.0  # errored but run completed
    assert score["final_score"] == 75.0

def test_evaluate_trace_exhausted():
    trace = TraceRecord(
        id="test-3",
        run_id="run-3",
        stop_reason="budget_exhausted",
        tool_result_summary="Error calling tool",
    )
    score = evaluate_trace(trace)
    assert score["completion_score"] == 0.0
    assert score["stop_quality_score"] == 0.0
    assert score["tool_correctness_score"] == 0.0
    assert score["error_recovery_score"] == 0.0  # errored and never completed
    assert score["final_score"] == 20.0

def test_evaluate_trace_loop_drift_decays_efficiency():
    """Deep loop_step tails (doom-loop signature) decay the efficiency term."""
    base = dict(run_id="run-ld", stop_reason="completed", tool_result_summary="ok")
    assert evaluate_trace(TraceRecord(id="a", loop_step=3, **base))["efficiency_score"] == 20.0
    assert evaluate_trace(TraceRecord(id="b", loop_step=9, **base))["efficiency_score"] == 10.0
    assert evaluate_trace(TraceRecord(id="c", loop_step=15, **base))["efficiency_score"] == 0.0

def test_classify_failure_none():
    trace = TraceRecord(
        id="test-4",
        run_id="run-4",
        stop_reason="completed",
        tool_result_summary="Success",
        tokens_before=0,
        tokens_after=1000,
    )
    score = evaluate_trace(trace)
    bucket = classify_failure(trace, score)
    assert bucket is None

def test_classify_failure_over_research():
    trace = TraceRecord(
        id="test-5",
        run_id="run-5",
        stop_reason="budget_exhausted",
        tool_result_summary="Success data fetched",
        tokens_before=0,
        tokens_after=1000, # not > 8000
    )
    score = evaluate_trace(trace)
    bucket = classify_failure(trace, score)
    assert bucket == "over_research"

def test_classify_failure_bad_arguments():
    trace = TraceRecord(
        id="test-6",
        run_id="run-6",
        stop_reason="blocked",
        tool_result_summary="Invalid ticker format error",
        tokens_before=0,
        tokens_after=1000,
    )
    score = evaluate_trace(trace)
    bucket = classify_failure(trace, score)
    assert bucket == "bad_arguments"

def test_classify_failure_loop_drift():
    trace = TraceRecord(
        id="test-7",
        run_id="run-7",
        stop_reason="blocked",
        tool_result_summary="Some data",
        loop_step=15,
    )
    score = evaluate_trace(trace)
    bucket = classify_failure(trace, score)
    assert bucket == "loop_drift"

def test_classify_failure_wrong_tool():
    trace = TraceRecord(
        id="test-8",
        run_id="run-8",
        stop_reason="blocked",
        tool_result_summary="Some data",
        tokens_before=0,
        tokens_after=1000,
    )
    score = evaluate_trace(trace)
    bucket = classify_failure(trace, score)
    assert bucket == "wrong_tool_selected"
