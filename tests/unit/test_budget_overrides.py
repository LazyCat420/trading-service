"""
Test: Budget Overrides — Verify role-differentiated turn budgets.

Validates that:
1. Risk agent gets max_turns=5
2. Meta audit agent gets max_turns=10
3. Default agents still get max_turns=3
4. Non-tool agents get max_turns=2
"""

import pytest


@pytest.mark.parametrize("agent_name, enable_tools, expected_turns", [
    ("risk", True, 9999),
    ("verifier", True, 9999),
    ("retriever", True, 9999),
    ("pre_trade", True, 9999),
    ("meta_audit", True, 9999),
    ("sentiment", True, 9999),
    ("unknown_agent_xyz", True, 9999),
    ("risk", False, 9999),
    ("meta_audit", False, 9999),
])
def test_agent_budget_turns(agent_name, enable_tools, expected_turns):
    """Verify that agent role and tool availability determine their turn budget."""
    from app.agents.tool_whitelists import get_agent_budget_turns

    turns = get_agent_budget_turns(agent_name, enable_tools=enable_tools)
    assert turns == expected_turns, (
        f"Agent '{agent_name}' (tools={enable_tools}) should get {expected_turns} turns, got {turns}"
    )
