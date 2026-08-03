"""Smart-money data was computed daily and read by nobody.

`smart_money_trade_scores` (79k rows) and `smart_money_performance` are
recomputed every morning by app/analytics/returns_engine.py and were exposed
through three registered tools — `get_smart_money_signal`, `_leads`,
`_leaderboard` — that appeared in NO agent whitelist. Registered is not
reachable: the desk paid for the compute and no LLM could ever see it.

Two fixes, tested here:
  1. the interactive chat agent is granted the three tools;
  2. the desk gets the actor-quality summary precomputed into its alt-data
     block, because this repo's measured telemetry is that analysts rarely
     spend a turn on an optional tool call (alt_data_block's own docstring).

The block reports a WITHIN-COHORT PERCENTILE and never the raw alpha — see
test_the_raw_alpha_is_not_shipped for why that distinction is load-bearing.
"""

import pytest

from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
from app.v3 import alt_data_block
from app.v3.alt_data_block import _ordinal, smart_money_quality_line


SMART_MONEY_TOOLS = [
    "get_smart_money_signal",
    "get_smart_money_leads",
    "get_smart_money_leaderboard",
]


# ── 1. the tools are reachable by someone ────────────────────────────

@pytest.mark.parametrize("tool", SMART_MONEY_TOOLS)
def test_smart_money_tool_is_granted_to_an_agent(tool):
    """Every registered smart-money tool must appear in at least one whitelist.

    A tool in the registry but in no whitelist is dead weight that LOOKS wired
    — it passes registration tests, renders in the tool catalog, and can never
    be called. That was true of all three of these for the tool's whole life.
    """
    holders = [a for a, tools in AGENT_TOOL_WHITELISTS.items() if tool in tools]
    assert holders, f"{tool} is registered but granted to no agent"


def test_registered_smart_money_tools_match_the_whitelisted_set():
    """Catch a fourth tool being added to the module and silently orphaned."""
    from app.tools import smart_money_tools  # noqa: F401  (registers on import)
    from app.tools import registry

    registered = {
        name for name in registry.tools
        if name.startswith("get_smart_money")
    }
    assert registered, "smart-money tools failed to register at all"
    granted = {
        t for tools in AGENT_TOOL_WHITELISTS.values() for t in tools
        if t.startswith("get_smart_money")
    }
    assert registered <= granted, (
        f"registered but granted to nobody: {sorted(registered - granted)}"
    )


# ── 2. the desk block ────────────────────────────────────────────────

def _fake_db(rows):
    class _Cur:
        def execute(self, *_a, **_k):
            return self

        def fetchall(self):
            return rows

    class _Ctx:
        def __enter__(self):
            return _Cur()

        def __exit__(self, *_):
            return False

    return lambda: _Ctx()


def test_quality_line_reports_percentile_not_alpha(monkeypatch):
    # (direction, actor_type, n, median_pctile, cohort_n)
    rows = [("buy", "fund", 11, 0.57, 15), ("sell", "congress", 2, 0.13, 164)]
    monkeypatch.setattr(alt_data_block, "get_db", _fake_db(rows))

    line = smart_money_quality_line("NVDA")
    assert "57th percentile" in line
    assert "13th percentile" in line
    assert "11 buy-side fund actor(s)" in line
    assert "n=15" in line and "n=164" in line
    # The caveat is part of the payload, not decoration: the desk must not
    # read a percentile as proof of skill.
    assert "not 'skilled'" in line
    assert "never alone" in line


def test_the_raw_alpha_is_not_shipped(monkeypatch):
    """The block must not quote absolute alpha. Measured 2026-08-03:

    the fund cohort is sec_collector's 26 hand-picked TRACKED_FUNDS — chosen
    because they are famous survivors — and shows +4 to +8pp "alpha" over
    1,500+ scored trades each. Quoting that per ticker made every mega-cap
    read +2 to +5.7pp on BOTH sides (NVDA's buy and sell medians landed within
    0.1pp of each other), i.e. the cohort's shared selection bias rendered as
    if it were a fact about the ticker. The population median for a rankable
    actor is -0.59pp with 48.6% positive, which is the honest picture.
    """
    rows = [("buy", "fund", 11, 0.57, 15)]
    monkeypatch.setattr(alt_data_block, "get_db", _fake_db(rows))

    line = smart_money_quality_line("NVDA")
    assert "pp" not in line, "a percentage-point alpha figure leaked into the block"
    assert "hand-picked" in line, "the selection-bias caveat must travel with it"


def test_quiet_ticker_returns_empty(monkeypatch):
    monkeypatch.setattr(alt_data_block, "get_db", _fake_db([]))
    assert smart_money_quality_line("NOBODYTRADESTHIS") == ""


def test_blank_ticker_never_queries():
    def _explode():
        raise AssertionError("must not open a connection for a blank ticker")

    assert smart_money_quality_line("") == ""
    assert smart_money_quality_line(None) == ""


def test_query_failure_is_fail_open(monkeypatch):
    """Every line in this block degrades to "" — never a pipeline error."""
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(alt_data_block, "get_db", _boom)
    assert smart_money_quality_line("NVDA") == ""


@pytest.mark.parametrize("n,expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
    (11, "11th"), (12, "12th"), (13, "13th"),
    (21, "21st"), (52, "52nd"), (73, "73rd"), (57, "57th"), (100, "100th"),
])
def test_ordinal(n, expected):
    assert _ordinal(n) == expected
