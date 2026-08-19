"""The benchmark's own guard rails.

A benchmark is measurement infrastructure, so its failure mode is worse than a
wrong answer: it is a CONFIDENT wrong answer. These tests pin the four
properties that, when they broke before, produced exactly that:

1. A refusal must never be scored as a fast success (2026-08-04: a 0-token
   window error returned in 1,046ms and was booked as "28x faster").
2. The cold start must not be folded into the median (an idle Jetson pays
   ~21s on its first call).
3. The concurrency phase must not run against a live cycle.
4. Prompt sizes come from the server's token count, never chars//4.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "jetson_benchmark.py"
_spec = importlib.util.spec_from_file_location("jetson_benchmark", _PATH)
jb = importlib.util.module_from_spec(_spec)
sys.modules["jetson_benchmark"] = jb
_spec.loader.exec_module(jb)


def _res(**kw):
    r = jb.CallResult(arm="t")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class TestClassificationIsFailClosed:
    def test_empty_response_is_a_failure_not_a_fast_win(self):
        """The minP signature: HTTP 200, zero content, ~1.5s."""
        outcome, ok, valid = jb._classify("", _res(elapsed_ms=1539))
        assert outcome == "EMPTY_RESPONSE"
        assert not ok and not valid

    def test_harness_error_text_is_a_failure(self):
        outcome, ok, _ = jb._classify(
            "⚠️ **Error:** context window is critically full", _res())
        assert outcome == "HARNESS_ERROR"
        assert not ok

    def test_transport_error_is_a_failure_even_with_text(self):
        outcome, ok, _ = jb._classify('{"ok": true}', _res(error="ReadTimeout"))
        assert outcome == "ERROR"
        assert not ok

    def test_prose_is_non_empty_but_not_a_valid_artifact(self):
        """The distinction the whitelist arm turns on."""
        outcome, ok, valid = jb._classify("Let me analyze the candidates...", _res())
        assert ok is True
        assert valid is False
        assert outcome == "NON_JSON"

    def test_real_json_artifact_is_a_success(self):
        outcome, ok, valid = jb._classify(
            'Here you go: {"selected_tickers": ["NVDA"], "rationale": "x"}', _res())
        assert (outcome, ok, valid) == ("SUCCESS", True, True)


class TestSummaryDoesNotLaunderTheColdStart:
    def test_cold_start_is_reported_separately_and_excluded(self):
        rs = [_res(elapsed_ms=21000, ok=True, valid_artifact=True)] + [
            _res(elapsed_ms=3000, ok=True, valid_artifact=True) for _ in range(4)
        ]
        s = jb._summarize("chat", rs)
        assert s["cold_start_ms"] == 21000
        assert s["median_ms_warm"] == 3000, "cold start leaked into the median"
        assert s["runs"] == 5

    def test_a_single_run_still_reports_rather_than_dividing_by_zero(self):
        s = jb._summarize("chat", [_res(elapsed_ms=1234, ok=True)])
        assert s["cold_start_ms"] == 1234 and s["median_ms_warm"] == 1234

    def test_failures_are_counted_not_dropped(self):
        rs = [_res(elapsed_ms=1500, ok=False, outcome="EMPTY_RESPONSE") for _ in range(3)]
        s = jb._summarize("agent", rs)
        assert s["non_empty"] == 0 and s["valid_artifact"] == 0


class TestConcurrencyIsGatedOnAnIdleCycle:
    """Unknown state must BLOCK, not proceed — a stress run against a live
    cycle degrades the desk it is supposed to be measuring."""

    def test_unreadable_pipeline_state_fails_closed(self, monkeypatch):
        # Poisoning the module makes `from scripts.migration.pg_connection import get_db`
        # raise, which is the only way to reach the except branch.
        monkeypatch.setitem(sys.modules, "scripts.migration.pg_connection", None)
        busy, why = jb.cycle_is_running()
        assert busy is True, "an unreadable pipeline_state must block the stress phase"
        # Strict: must be the REFUSAL message, not a successful status read.
        # Accepting either would pass whether or not the guard exists.
        assert "refusing to stress" in why

    @pytest.mark.parametrize("status,expected", [
        ("running", True), ("starting", True), ("analyzing", True),
        ("stopped", False), ("idle", False), ("completed", False),
    ])
    def test_each_status_is_classified(self, status, expected, monkeypatch):
        """Pinned per-value: 'analyzing' is a live cycle and reads as safe to
        anything that only checks for the literal 'running'."""
        class _DB:
            def execute(self, *a, **k): pass
            def fetchall(self): return [("cycle-x", status)]

        class _Ctx:
            def __enter__(self): return _DB()
            def __exit__(self, *a): return False

        mod = type(sys)("scripts.migration.pg_connection")
        mod.get_db = lambda: _Ctx()
        monkeypatch.setitem(sys.modules, "scripts.migration.pg_connection", mod)
        busy, _ = jb.cycle_is_running()
        assert busy is expected


class TestSampleSizeDefault:
    def test_default_runs_can_separate_arms(self):
        """n=3 cannot: 2/3 is compatible with a true rate from ~15% to ~95%."""
        assert jb.DEFAULT_RUNS >= 10


class TestPromptSizingUsesServerTokens:
    def test_no_chars_over_four_heuristic_anywhere(self):
        """chars//4 is off by 2.5x on numeric payloads and silently overshoots
        the 65k window, which then reads as a Jetson failure."""
        src = _PATH.read_text()
        assert "// 4" not in src and "/ 4" not in src

    def test_token_counts_come_from_the_done_event(self):
        src = _PATH.read_text()
        assert 'usage.get("inputTokens")' in src


class TestKnownBadArmSurvives:
    def test_the_reproducer_arm_is_still_available(self):
        """Deleting this arm would make a future 'it works now' unfalsifiable."""
        src = _PATH.read_text()
        assert "agent-nominp" in src
        assert "min_p=None" in src


class TestCorpusIsReal:
    def test_it_refuses_to_invent_prompts(self):
        """An empty corpus must abort, not fall back to synthetic fixtures —
        those measure a distribution the desk does not have."""
        src = _PATH.read_text()
        assert "model_shadow_runs" in src
        assert "would be" in src and "invented prompts" in src
