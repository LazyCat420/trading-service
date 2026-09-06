"""Tests for Jetson prompt, tool denial, and turn budget optimizations."""

from app.v3.prism_registration import _V3_DENIED_TOOLS, _V3_TOOL_POLICIES, _V3_COMMON_GUIDELINES
from app.v3.agents.debate_judge import TOOL_WHITELIST as JUDGE_TOOLS, SYSTEM_PROMPT as JUDGE_PROMPT
from app.v3.agents.bull_defense import TOOL_WHITELIST as DEFENSE_TOOLS, SYSTEM_PROMPT as DEFENSE_PROMPT
from app.agents.tool_whitelists import get_agent_budget_turns, AGENT_BUDGET_OVERRIDES


def test_think_is_no_longer_denied():
    """Reversed 2026-09-06, on measurement.

    The deny landed on 09-02 to save turns and was kept for four days. Over
    that window `agent_tool_telemetry` recorded 301 POLICY_DENIED `think` calls
    across 261 agent runs — 68.9% of every agent run that made a tool call —
    because the models kept calling it despite rule 7 naming it. And the turn
    was spent either way: loops minus non-think tool calls averaged 1.83 in
    runs with a denied think against 0.85 without (mean 1.14 think calls), and
    2.21 against 0.93 (mean 1.26) while it was still allowed. The policy is
    evaluated after the model has already emitted the call, so denying it never
    recovered the turn — it only replaced a scratchpad with an error. Outcomes
    were unchanged on both sides (224/226 SUCCESS denied, 504/504 allowed).

    See also test_a_denied_tool_is_never_one_the_canary_ignores.py: `think` was
    simultaneously in `_META_TOOLS`, so none of that waste was ever logged.
    """
    assert "think" not in _V3_DENIED_TOOLS
    policy_names = [p["name"] for p in _V3_TOOL_POLICIES]
    assert "deny(think)" not in policy_names


def test_the_genuinely_dangerous_tools_are_still_denied():
    """Reversing `think` must not soften the lockdown around it."""
    for tool in ("execute_command", "execute_javascript", "execute_skill",
                 "write_file", "query_datastore"):
        assert tool in _V3_DENIED_TOOLS
        assert f"deny({tool})" in [p["name"] for p in _V3_TOOL_POLICIES]


def test_common_guidelines_syntax_rules():
    assert "execute_python" in _V3_COMMON_GUIDELINES
    assert "True, False, and None" in _V3_COMMON_GUIDELINES
    assert "Do not call think" not in _V3_COMMON_GUIDELINES


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
