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


# ── error_class discriminator ──────────────────────────────────────────────
# Harness defects and bad market calls both land in failure_buckets. Only the
# former may trigger an automated code repair.

def test_error_class_separates_engineering_from_market():
    from app.autoresearch.eval_engine import (
        classify_error_class,
        ERROR_CLASS_ENGINEERING,
        ERROR_CLASS_MARKET,
        ERROR_CLASS_UNCLASSIFIED,
    )

    assert classify_error_class("over_research") == ERROR_CLASS_ENGINEERING
    assert classify_error_class("bad_arguments") == ERROR_CLASS_ENGINEERING
    assert classify_error_class("loop_drift") == ERROR_CLASS_ENGINEERING

    # A losing HOLD is a market outcome — patching source code cannot fix it.
    assert classify_error_class("hold_bias") == ERROR_CLASS_MARKET

    # The catch-all bucket is too noisy to justify an automated code change.
    assert classify_error_class("wrong_tool_selected") == ERROR_CLASS_UNCLASSIFIED

    # A passing run has no class at all.
    assert classify_error_class(None) is None


def test_hold_bias_never_triggers_repair():
    """Regression guard for the conflation this discriminator exists to fix."""
    from app.autoresearch.eval_engine import classify_error_class, ERROR_CLASS_ENGINEERING

    trace = TraceRecord(
        id="test-hold",
        run_id="run-hold",
        stop_reason="blocked",   # scores < 70 so a bucket is actually assigned
        tool_result_summary="Some data",
        decision_action="HOLD",
        decision_confidence=80,
        pnl_pct=-9.5,            # a real loss, but not a code defect
        tokens_before=0,
        tokens_after=1000,
    )
    score = evaluate_trace(trace)
    bucket = classify_failure(trace, score)
    assert bucket == "hold_bias"
    assert classify_error_class(bucket) != ERROR_CLASS_ENGINEERING
