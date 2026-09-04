"""Turn budgets: the properties, not a transcription of the table.

This file used to hardcode a copy of `AGENT_BUDGET_OVERRIDES` in a parametrize
list. On 2026-09-02 commit `51892a9` ("expand agent turn budgets") deliberately
raised FOUR budgets — bull 3→5, bear 3→5, bull_defense 3→4, judge 3→4 — and
updated a different test file. Two of the four then failed here and two did not,
because the list covered 9 of the table's 14 entries: which drift this file
caught was an accident of who typed which rows.

Its companion `test_budget_table_stays_in_sync` was worse than useless. It read
`AGENT_BUDGET_OVERRIDES` and asserted `get_agent_budget_turns` returned the same
value — which is `AGENT_BUDGET_OVERRIDES.get(k)`. It could not fail.

So the exact post-`51892a9` numbers (5/5/4/4) are pinned ONCE, in
`test_jetson_prompt_optimizations.py::test_agent_turn_budgets`, next to the
prompt changes that motivated them. What lives here is everything that must be
true of the table however the numbers move, derived FROM the table.

Deliberately not duplicated (assert it where it already is):
  * every tool-carrying v3 agent has an entry — `test_every_whitelisted_v3_agent_has_a_budget_entry`, below
  * no tool-carrying agent gets 1 turn — `test_adaptive_fair_debate.py::test_a_tool_carrying_agent_never_has_a_single_turn_budget`
  * junior >= 7 and tools-off == 1 — `test_junior_analyst_audit.py:93-100`
  * bull/bear/defense/judge exact values — `test_jetson_prompt_optimizations.py::test_agent_turn_budgets`
"""

import pytest

from app.agents.tool_whitelists import (
    AGENT_BUDGET_OVERRIDES,
    _DEFAULT_BUDGET,
    get_agent_budget_turns,
)


# ── Values whose exact number carries a reason ──────────────────────────────
#
# A budget is pinned here only when a specific measurement chose it, and the
# reason is the point of the row. Everything else is covered by the properties
# below, so re-tuning a budget does not mean editing a transcription.
_PINNED = [
    # An interactive session, not a pipeline step; it is allowed to explore.
    ("user_chat", 15, "interactive budget"),
    # 7 (from 5) on 2026-07-24: over 56 runs, 96% finished at the 5-6 loop
    # ceiling, so the budget WAS the normal path and step 3 of the documented
    # loop — "TRACE one lead depth-first" — was structurally unreachable.
    ("v3_junior_analyst", 7, "2026-07-24 audit: 96% were at the old ceiling"),
    # 12 (from 7) on 2026-07-19: every SUCCESSFUL run landed on exactly 7
    # loops, and runs that hit it emitted a pseudo tool call instead of the
    # artifact. Multi-source lookups precede its report.
    ("v3_fundamental_analyst", 12, "2026-07-19: successes pinned at the ceiling"),
    # 14 (from 12) on 2026-07-21: the portfolio-math wave added GARCH and
    # HRP/covariance calls to the quant's documented loop.
    ("v3_quant_analyst", 14, "2026-07-21 portfolio-math wave added GARCH + HRP"),
    # 6: ONE mandatory tool call (screener_query for sector comps, which
    # doctrine rule 7 depends on) plus a whiteboard annotate; everything else
    # is precomputed into the prompt by app/quant/valuation_block.py.
    ("v3_valuation_analyst", 6, "one mandatory screener call + annotate"),
]


@pytest.mark.parametrize("agent_name, expected, reason", _PINNED)
def test_pinned_budgets_with_a_documented_reason(agent_name, expected, reason):
    turns = get_agent_budget_turns(agent_name, enable_tools=True)
    assert turns == expected, (
        f"{agent_name} is pinned at {expected} ({reason}); got {turns}. "
        "If the change is deliberate, update the reason here — do not just "
        "change the number."
    )


# ── Properties of the table, derived from the table ─────────────────────────

def test_no_override_is_the_default():
    """An entry equal to the 9999 default is an entry that does nothing.

    Three agents have reached production on that default one at a time
    (v3_bull_defense 08-05, v3_delta_analyst 08-09, and the near-miss the
    portfolio_manager comment records), each printing "TURN BUDGET: 9999" into
    its own prompt. `test_every_whitelisted_v3_agent_has_a_budget_entry`
    catches a MISSING entry; this catches a present-but-inert one.
    """
    inert = {a: b for a, b in AGENT_BUDGET_OVERRIDES.items() if b >= _DEFAULT_BUDGET}
    assert not inert, f"budget entries equal to the default do nothing: {inert}"


def test_every_override_allows_a_call_and_an_answer():
    """A tool-carrying agent needs at least one turn to call and one to answer.

    Wider scope than test_adaptive_fair_debate's version, deliberately: that
    one discovers tool-carrying v3 agents by importing their modules, this one
    covers every key in the table including user_chat and any future non-v3
    entry. A budget of 1 gave v3_bull_defense a single iteration and it
    returned 49- and 94-char non-artifacts for RNGR.
    """
    too_small = {a: b for a, b in AGENT_BUDGET_OVERRIDES.items() if b < 2}
    assert not too_small, f"a budget below 2 cannot call a tool and answer: {too_small}"


def test_unknown_agents_fall_back_to_the_default():
    """An unmapped agent must not inherit some other agent's budget."""
    assert get_agent_budget_turns("unknown_agent_xyz", enable_tools=True) == _DEFAULT_BUDGET


def test_tools_off_is_a_single_turn_for_everyone():
    """No tools means one generation turn, whatever the table says."""
    for agent_name in list(AGENT_BUDGET_OVERRIDES) + ["unknown_agent_xyz", ""]:
        assert get_agent_budget_turns(agent_name, enable_tools=False) == 1, agent_name


def test_delta_analyst_has_a_real_budget():
    """v3_delta_analyst was the THIRD agent to silently inherit the 9999
    default (after v3_bull_defense on 08-05 and the near-miss the PM comment
    records): it carries a 4-tool whitelist, so enable_tools is True, and
    with no override entry its prompt printed "TURN BUDGET: 9999" in
    production (seen live on GEN, cycle-v3-1786297004, 2026-08-09)."""
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

    agents_dir = (
        Path(__file__).resolve().parents[2] / "app" / "v3" / "agents"
    )
    def _assigned_names(node):
        """Both assignment forms. Only ast.Assign was matched until
        2026-08-10, so `TOOL_WHITELIST: list[str] = [...]` — an ast.AnnAssign
        — was invisible, and five of the thirteen agents (bull, bear,
        bull_defense, board_of_directors, decision) were silently exempt from
        the very check this test exists to make. test_cycle_candidates.py
        already handled both; this one did not.
        """
        if isinstance(node, ast.Assign):
            return [getattr(t, "id", "") for t in node.targets]
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            return [getattr(node.target, "id", "")]
        return []

    missing = []
    scanned = 0
    with_whitelist = 0
    for mod in sorted(agents_dir.glob("*.py")):
        if mod.name == "__init__.py":
            continue
        tree = ast.parse(mod.read_text())
        scanned += 1
        has_whitelist = False
        agent_name = None
        for node in tree.body:
            for name in _assigned_names(node):
                if name == "TOOL_WHITELIST":
                    try:
                        has_whitelist = bool(ast.literal_eval(node.value))
                    except Exception:
                        has_whitelist = True  # non-literal → assume tools
                if name == "AGENT_NAME":
                    try:
                        agent_name = ast.literal_eval(node.value)
                    except Exception:
                        pass
        if has_whitelist:
            with_whitelist += 1
        if has_whitelist and agent_name and agent_name not in AGENT_BUDGET_OVERRIDES:
            missing.append(f"{agent_name} ({mod.name})")

    # Floors, so the guard cannot go quiet again. It read 8 of 13 agents for
    # however long the AnnAssign hole was open and reported success the whole
    # time; a count that has to be met turns that into a failure.
    assert scanned >= 13, f"only found {scanned} v3 agent modules — wrong path?"
    assert with_whitelist >= 10, (
        f"only {with_whitelist} of {scanned} agents parsed as tool-carrying; "
        "the TOOL_WHITELIST matcher has stopped seeing a declaration form"
    )

    assert not missing, (
        "Tool-carrying v3 agents with NO budget entry — they will run with "
        f"the 9999 default: {missing}"
    )


def test_the_whitelist_matcher_sees_both_assignment_forms():
    """Negative control for the scan above. The plain-assignment form was
    always matched; the annotated one was not, and that is what let five
    agents through."""
    import ast

    for src in (
        "TOOL_WHITELIST = ['a']\n",
        "TOOL_WHITELIST: list[str] = ['a']\n",
    ):
        tree = ast.parse(src)
        names = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names += [getattr(t, "id", "") for t in node.targets]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                names.append(getattr(node.target, "id", ""))
        assert "TOOL_WHITELIST" in names, f"matcher missed: {src!r}"
