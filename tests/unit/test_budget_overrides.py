"""
Test: Budget Overrides — Verify role-differentiated turn budgets.

Validates that (per AGENT_BUDGET_OVERRIDES in app/agents/tool_whitelists.py):
1. V3 pipeline agents get real limits (not V2's 9999)
2. user_chat gets a generous interactive budget
3. Unknown agents fall back to the 9999 default
4. Non-tool agents always get max_turns=1
"""

import pytest


@pytest.mark.parametrize("agent_name, enable_tools, expected_turns", [
    ("user_chat", True, 15),
    ("v3_junior_analyst", True, 5),
    ("v3_fundamental_analyst", True, 12),
    ("v3_quant_analyst", True, 14),
    ("v3_bull_agent", True, 3),
    ("v3_debate_judge", True, 3),
    ("v3_regime_engine", True, 5),
    ("v3_board_of_directors", True, 5),
    ("unknown_agent_xyz", True, 9999),
    ("v3_quant_analyst", False, 1),
    ("user_chat", False, 1),
])
def test_agent_budget_turns(agent_name, enable_tools, expected_turns):
    """Verify that agent role and tool availability determine their turn budget."""
    from app.agents.tool_whitelists import get_agent_budget_turns

    turns = get_agent_budget_turns(agent_name, enable_tools=enable_tools)
    assert turns == expected_turns, (
        f"Agent '{agent_name}' (tools={enable_tools}) should get {expected_turns} turns, got {turns}"
    )


def test_budget_table_stays_in_sync():
    """Every override entry must be resolvable through get_agent_budget_turns,
    so this test fails loudly if the table shape changes."""
    from app.agents.tool_whitelists import AGENT_BUDGET_OVERRIDES, get_agent_budget_turns

    for agent_name, expected in AGENT_BUDGET_OVERRIDES.items():
        assert get_agent_budget_turns(agent_name, enable_tools=True) == expected
