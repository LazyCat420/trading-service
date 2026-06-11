"""
Test: Budget Overrides — Verify role-differentiated turn budgets.

Validates that:
1. Risk agent gets max_turns=8
2. Meta audit agent gets max_turns=12
3. Default agents get max_turns=9999
4. Non-tool agents get max_turns=1
"""

import pytest


@pytest.mark.parametrize("agent_name, enable_tools, expected_turns", [
    ("risk", True, 8),
    ("verifier", True, 5),
    ("retriever", True, 5),
    ("pre_trade", True, 8),
    ("meta_audit", True, 12),
    ("sentiment", True, 8),
    ("unknown_agent_xyz", True, 9999),
    ("risk", False, 1),
    ("meta_audit", False, 1),
])
def test_agent_budget_turns(agent_name, enable_tools, expected_turns):
    """Verify that agent role and tool availability determine their turn budget."""
    from app.agents.tool_whitelists import get_agent_budget_turns

    turns = get_agent_budget_turns(agent_name, enable_tools=enable_tools)
    assert turns == expected_turns, (
        f"Agent '{agent_name}' (tools={enable_tools}) should get {expected_turns} turns, got {turns}"
    )
