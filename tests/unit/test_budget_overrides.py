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
    # 7 since the 2026-07-24 audit: 96% of runs finished at the old 5-turn
    # ceiling, so the budget was the normal path and the depth-first lead trace
    # could never run.
    ("v3_junior_analyst", True, 7),
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


def test_delta_analyst_has_a_real_budget():
    """v3_delta_analyst was the THIRD agent to silently inherit the 9999
    default (after v3_bull_defense on 08-05 and the near-miss the PM comment
    records): it carries a 4-tool whitelist, so enable_tools is True, and
    with no override entry its prompt printed "TURN BUDGET: 9999" in
    production (seen live on GEN, cycle-v3-1786297004, 2026-08-09)."""
    from app.agents.tool_whitelists import get_agent_budget_turns

    assert get_agent_budget_turns("v3_delta_analyst", enable_tools=True) == 5


def test_every_whitelisted_v3_agent_has_a_budget_entry():
    """The structural fix for the missing-entry trap: SCAN the v3 agent
    modules for a TOOL_WHITELIST and require an AGENT_BUDGET_OVERRIDES entry
    for each. Three agents have fallen into the gap one at a time; this makes
    the fourth a CI failure instead of a production discovery.

    An agent module without a TOOL_WHITELIST is exempt: enable_tools=False
    routes it to the single-turn path and the default is unreachable.
    """
    import ast
    from pathlib import Path

    from app.agents.tool_whitelists import AGENT_BUDGET_OVERRIDES

    agents_dir = (
        Path(__file__).resolve().parents[2] / "app" / "v3" / "agents"
    )
    missing = []
    for mod in sorted(agents_dir.glob("*.py")):
        if mod.name == "__init__.py":
            continue
        tree = ast.parse(mod.read_text())
        has_whitelist = False
        agent_name = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if getattr(tgt, "id", "") == "TOOL_WHITELIST":
                        try:
                            has_whitelist = bool(ast.literal_eval(node.value))
                        except Exception:
                            has_whitelist = True  # non-literal → assume tools
                    if getattr(tgt, "id", "") == "AGENT_NAME":
                        try:
                            agent_name = ast.literal_eval(node.value)
                        except Exception:
                            pass
        if has_whitelist and agent_name and agent_name not in AGENT_BUDGET_OVERRIDES:
            missing.append(f"{agent_name} ({mod.name})")

    assert not missing, (
        "Tool-carrying v3 agents with NO budget entry — they will run with "
        f"the 9999 default: {missing}"
    )
