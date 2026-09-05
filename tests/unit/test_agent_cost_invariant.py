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
