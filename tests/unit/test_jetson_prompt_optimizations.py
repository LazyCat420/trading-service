"""Tests for Jetson prompt, tool denial, and turn budget optimizations."""

from app.v3.prism_registration import _V3_DENIED_TOOLS, _V3_TOOL_POLICIES, _V3_COMMON_GUIDELINES
from app.v3.agents.debate_judge import TOOL_WHITELIST as JUDGE_TOOLS, SYSTEM_PROMPT as JUDGE_PROMPT
from app.v3.agents.bull_defense import TOOL_WHITELIST as DEFENSE_TOOLS, SYSTEM_PROMPT as DEFENSE_PROMPT
from app.agents.tool_whitelists import get_agent_budget_turns, AGENT_BUDGET_OVERRIDES


def test_think_tool_is_denied():
    assert "think" in _V3_DENIED_TOOLS
    policy_names = [p["name"] for p in _V3_TOOL_POLICIES]
    assert "deny(think)" in policy_names


def test_common_guidelines_syntax_rules():
    assert "execute_python" in _V3_COMMON_GUIDELINES
    assert "True, False, and None" in _V3_COMMON_GUIDELINES
    assert "Do not call think" in _V3_COMMON_GUIDELINES


def test_debate_judge_grounding_and_whitelists():
    assert JUDGE_TOOLS == ["whiteboard_read"]
    assert "DATA ALREADY EMBEDDED — DO NOT RE-FETCH" in JUDGE_PROMPT


def test_bull_defense_grounding():
    assert DEFENSE_TOOLS == ["whiteboard_read"]
    assert "DATA ALREADY EMBEDDED — DO NOT RE-FETCH" in DEFENSE_PROMPT


def test_agent_turn_budgets():
    assert get_agent_budget_turns("v3_bull_agent", enable_tools=True) == 5
    assert get_agent_budget_turns("v3_bear_agent", enable_tools=True) == 5
    assert get_agent_budget_turns("v3_bull_defense", enable_tools=True) == 4
    assert get_agent_budget_turns("v3_debate_judge", enable_tools=True) == 4
