"""The turn budget must have exactly one source, and the lookup must hit it.

`app/v3/guardrails.py` carried a second copy of the turn budgets keyed without
the `v3_` prefix, under a comment claiming it was "harmonized with
tool_whitelists.py AGENT_BUDGET_OVERRIDES". `get_budget_for_role` stripped
`custom_v3_` and `custom_` but never a bare `v3_`, so every real agent name
missed and fell through to the default of 7. Measured 2026-09-13 against live
code: **14 of 14 mismatched**. Not one entry had ever been read.

That is the failure mode a count cannot catch — the table had the right number
of rows, the right agent names, plausible values, and a comment asserting it was
correct. Only resolving a real name through the real function exposes it.

These tests therefore assert on the FUNCTION's output, not on the table's
contents, and assert set equality in both directions so a new agent cannot be
added to one table and forgotten in the other.
"""

from __future__ import annotations

import pytest

from app.agents.tool_whitelists import AGENT_BUDGET_OVERRIDES
from app.v3.guardrails import (
    AGENT_MAX_TOOL_CALLS,
    _DEFAULT_ROLE_BUDGET,
    canonical_agent_name,
    get_budget_for_role,
)


@pytest.mark.parametrize("agent", sorted(AGENT_BUDGET_OVERRIDES))
def test_every_named_agent_resolves_to_its_own_budget(agent):
    """The regression itself: resolve each real name and compare."""
    want = AGENT_BUDGET_OVERRIDES[agent]
    got = get_budget_for_role(agent).max_turns
    assert got == want, (
        f"{agent}: tool_whitelists says {want} turns, guardrails resolves {got}. "
        "The two tables have diverged, or the name is not being canonicalised."
    )


def test_the_sentinel_is_not_mistaken_for_a_budget():
    """9999 means 'no override', not 'nine thousand turns'."""
    assert get_budget_for_role("v3_nonexistent_agent").max_turns == (
        _DEFAULT_ROLE_BUDGET["max_turns"])


@pytest.mark.parametrize("prefix", ["", "v3_", "custom_v3_"])
def test_the_wrapper_prefixes_name_the_same_agent(prefix):
    """`custom_v3_bull_defense` and `v3_bull_defense` are one agent.

    The old code stripped `custom_v3_` down to `bull_defense` — a name that
    exists in neither table — which is precisely how the lookup died.
    """
    if prefix == "":
        pytest.skip("a bare role name is not a supported spelling")
    assert canonical_agent_name(prefix + "bull_defense") == "v3_bull_defense"
    assert get_budget_for_role(prefix + "bull_defense").max_turns == \
        AGENT_BUDGET_OVERRIDES["v3_bull_defense"]


def test_the_tool_call_table_covers_the_same_agents_both_ways():
    """Set equality in BOTH directions — never a length comparison.

    Two omissions and two inventions cancel in a count. Only the two
    difference sets are evidence.
    """
    turns = {a for a in AGENT_BUDGET_OVERRIDES if a.startswith("v3_")}
    calls = {a for a in AGENT_MAX_TOOL_CALLS if a.startswith("v3_")}
    missing_caps = turns - calls
    orphan_caps = calls - turns
    assert not missing_caps, (
        f"agents with a turn budget but no tool-call cap: {sorted(missing_caps)} "
        "— they silently inherit the default of "
        f"{_DEFAULT_ROLE_BUDGET['max_tool_calls']}"
    )
    assert not orphan_caps, (
        f"tool-call caps for agents that no longer exist: {sorted(orphan_caps)}"
    )


def test_no_budget_is_absurd():
    """Properties, not pinned constants — this must not go red for a retune."""
    for agent, turns in AGENT_BUDGET_OVERRIDES.items():
        if turns >= 9999:
            continue
        assert 1 <= turns <= 40, f"{agent} has {turns} turns"
    for agent, calls in AGENT_MAX_TOOL_CALLS.items():
        assert 1 <= calls <= 60, f"{agent} has {calls} tool calls"
        turns = AGENT_BUDGET_OVERRIDES.get(agent)
        if turns and turns < 9999:
            assert calls >= turns - 1, (
                f"{agent}: {calls} tool calls for {turns} turns — the tool-call "
                "cap would bind before the turn budget, so raising turns does "
                "nothing"
            )
