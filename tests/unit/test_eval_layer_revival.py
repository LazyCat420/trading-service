"""Regression tests for the 2026-07-16 judge/eval layer revival.

The eval layer starved when the vllm_client → SDK migration (fa7cee3) severed
rlm_wrapper's caller: agent_traces / llm_audit_logs / context_blobs all stopped
being written on 2026-06-25. These tests pin the revived producer contract and
the rubric vocabulary fix.
"""

from unittest.mock import MagicMock, patch

from app.autoresearch.eval_engine import TraceRecord, evaluate_trace
from app.autoresearch.trace_writer import write_agent_trace


def _trace(**overrides):
    base = dict(
        id="t1",
        run_id="cycle-v3-test",
        stop_reason="success",
        tokens_before=0,
        tokens_after=100,
    )
    base.update(overrides)
    return TraceRecord(**base)


class TestRubricVocabulary:
    def test_success_counts_as_completion(self):
        # Historical producer wrote 'success'; rubric only accepted
        # 'completed' — completion scored 0 on 100% of graded history.
        assert evaluate_trace(_trace(stop_reason="success"))["completion_score"] == 40.0

    def test_completed_counts_as_completion(self):
        assert evaluate_trace(_trace(stop_reason="completed"))["completion_score"] == 40.0

    def test_error_scores_zero_completion(self):
        assert evaluate_trace(_trace(stop_reason="error"))["completion_score"] == 0.0


class TestTraceWriter:
    def test_run_id_is_cycle_id(self):
        # eval_engine.process_pending_traces zips t.run_id into its cycle_id
        # slot and looks up decision_outcomes by it — a composite run_id would
        # silently break the hold_bias classification.
        captured = {}

        class FakeCursor:
            def execute(self, sql, params=None):
                captured["sql"] = sql
                captured["params"] = params

            def commit(self):
                pass

        class FakeCtx:
            def __enter__(self):
                return FakeCursor()

            def __exit__(self, *a):
                return False

        with patch("app.autoresearch.trace_writer.get_db", return_value=FakeCtx()):
            write_agent_trace(
                cycle_id="cycle-v3-123",
                ticker="TSM",
                agent_name="v3_quant_analyst",
                tool_name="get_market_data",
                tool_args={"ticker": "TSM"},
                tool_result="{...}",
                failed=False,
                latency_ms=42,
            )

        params = captured["params"]
        assert params[1] == "cycle-v3-123"  # run_id == cycle_id
        assert params[13] == "completed"    # stop_reason matches rubric vocab

    def test_failed_call_stops_with_error(self):
        captured = {}

        class FakeCursor:
            def execute(self, sql, params=None):
                captured["params"] = params

            def commit(self):
                pass

        class FakeCtx:
            def __enter__(self):
                return FakeCursor()

            def __exit__(self, *a):
                return False

        with patch("app.autoresearch.trace_writer.get_db", return_value=FakeCtx()):
            write_agent_trace(
                cycle_id="c", ticker="T", agent_name="a",
                tool_name="t", tool_args={}, tool_result="boom",
                failed=True, latency_ms=1,
            )
        assert captured["params"][13] == "error"
        assert captured["params"][7].startswith("ERROR: ")

    def test_never_raises_on_db_failure(self):
        with patch("app.autoresearch.trace_writer.get_db", side_effect=RuntimeError("db down")):
            # Must swallow — telemetry can't be allowed to break agent runs.
            write_agent_trace(
                cycle_id="c", ticker="T", agent_name="a",
                tool_name="t", tool_args={}, tool_result="r",
                failed=False, latency_ms=1,
            )
