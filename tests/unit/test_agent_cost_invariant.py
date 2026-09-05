"""Unit tests for the agent cost invariant (_check_agent_cost).

Enforces that an agent burning substantial tokens (> 20,000) while running
only a single loop (<= 1.0) without research triggers the KIND_AGENT_COST_NO_RESEARCH
invariant violation.
"""

from unittest.mock import patch
from app.v3.invariants import _check_agent_cost, KIND_AGENT_COST_NO_RESEARCH, COST_NO_RESEARCH_TOKENS


def test_cost_no_research_tokens_threshold_is_sensitive_to_single_turn_bursts():
    """COST_NO_RESEARCH_TOKENS must be <= 25,000 so ~30k token single-turn runs are caught."""
    assert COST_NO_RESEARCH_TOKENS <= 25_000, f"Threshold {COST_NO_RESEARCH_TOKENS} is too high to catch 30k token single-turn runs"


def test_check_agent_cost_flags_single_turn_30k_token_agent():
    mock_agg = [
        {"_id": "v3_junior_analyst", "tok": 32000, "loops": 1.0}
    ]
    with patch("app.db.mongo_store.aggregate", return_value=mock_agg), \
         patch("app.db.mongo_store.insert_docs", return_value=1):
        violations = _check_agent_cost("cycle-v3-mock-test")
        assert len(violations) == 1
        assert violations[0] == KIND_AGENT_COST_NO_RESEARCH


def test_check_agent_cost_passes_healthy_multi_loop_agent():
    mock_agg = [
        {"_id": "v3_junior_analyst", "tok": 32000, "loops": 3.0}
    ]
    with patch("app.db.mongo_store.aggregate", return_value=mock_agg):
        violations = _check_agent_cost("cycle-v3-mock-test")
        assert len(violations) == 0


def test_check_agent_cost_exempts_synthesizers_below_150k():
    mock_agg = [
        {"_id": "v3_decision_synthesizer", "tok": 32000, "loops": 1.0}
    ]
    with patch("app.db.mongo_store.aggregate", return_value=mock_agg):
        violations = _check_agent_cost("cycle-v3-mock-test")
        assert len(violations) == 0


class TestCachedAgentsAreNotFlagged:
    """A cached agent's one loop is its design, not an anomaly.

    MEASURED 2026-09-05 over 1,633 agent runs / 16 days at the shipped 20k
    threshold: 193 fires, 62 of them on cycles that researched normally, and
    `v3_regime_engine` was 29 of those 62 (47%). The regime engine classifies
    once and every later ticker reuses the result, so one loop at ~26k prompt
    tokens is correct. `_NON_RESEARCHING_AGENTS` already declared this for the
    cycle-level check; `_check_agent_cost` did not consult it, so two checks in
    one module disagreed about the same fact.
    """

    def _drive(self, monkeypatch, docs):
        from app.v3 import invariants

        recorded = []
        monkeypatch.setattr(invariants.mongo_store, "aggregate",
                            lambda *a, **k: docs, raising=False)
        monkeypatch.setattr(
            invariants, "record_violation",
            lambda kind, **d: (recorded.append((kind, d)), kind)[1],
        )
        return invariants._check_agent_cost("cycle-test"), recorded

    def test_the_regime_engine_is_not_flagged_for_one_cached_loop(self, monkeypatch):
        """The real LULU row from cycle-v3-1788642086: 26,009 tok at 1 loop."""
        from app.v3 import invariants

        out, rec = self._drive(monkeypatch, [
            {"_id": "v3_regime_engine", "tok": 26_009, "loops": 1.0},
        ])
        assert out == [], f"cached agent flagged: {rec}"

    def test_contradiction_shadow_is_not_flagged_either(self, monkeypatch):
        out, _ = self._drive(monkeypatch, [
            {"_id": "contradiction_shadow", "tok": 40_000, "loops": 0.0},
        ])
        assert out == []

    def test_a_tool_carrying_agent_at_one_loop_still_flags(self, monkeypatch):
        """The exclusion must not swallow the signal it was built for.

        SNOW's quant analyst really did run 1 loop at 26,655 tokens. Whether
        that is signal is an open question, but it must stay VISIBLE — this is
        the guard that the fix narrowed nothing beyond the two cached agents.
        """
        from app.v3 import invariants

        out, _ = self._drive(monkeypatch, [
            {"_id": "v3_quant_analyst", "tok": 26_655, "loops": 1.0},
        ])
        assert out == [invariants.KIND_AGENT_COST_NO_RESEARCH]

    def test_the_sglang_outage_shape_still_flags(self, monkeypatch):
        """The true positive this check exists for: ~40k at exactly 1 loop."""
        from app.v3 import invariants

        out, _ = self._drive(monkeypatch, [
            {"_id": "v3_junior_analyst", "tok": 37_260, "loops": 1.0},
            {"_id": "v3_bull_agent", "tok": 48_538, "loops": 1.0},
        ])
        assert len(out) == 2

    def test_both_checks_read_the_same_set(self):
        """The defect was two checks disagreeing; pin that they cannot again."""
        import inspect

        from app.v3 import invariants

        for fn in (invariants._check_agent_cost, invariants._check_cycle_did_research):
            assert "_NON_RESEARCHING_AGENTS" in inspect.getsource(fn), (
                f"{fn.__name__} does not consult the shared set"
            )
