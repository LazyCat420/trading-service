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

# The percentile used to arrive precomputed from SQL. It is derived in Python
# now — from `smart_money_performance` (the cohort's 1y alphas) and
# `smart_money_trade_scores` (who traded THIS ticker) — so the fixtures below
# are the two raw collections and the ranking itself is under test.
#
# rank/(cohort_n - 1) is the percentile, so a fund cohort of 15 puts the 9th
# lowest alpha at 8/14 = 0.571 -> "57th", and a congress cohort of 164 puts the
# 22nd lowest at 21/163 = 0.129 -> "13th".


def _cohort(actor_type: str, n: int, subject_rank: int, subject_id: str):
    """`n` actors of `actor_type` with distinct ascending alphas.

    `subject_id` is placed at `subject_rank` (0-based) so its percentile is a
    stated number rather than an accident of the fixture's ordering.
    """
    ids = [f"{actor_type}-{i}" for i in range(n)]
    ids[subject_rank] = subject_id
    return [
        {"actor_type": actor_type, "actor_id": aid, "horizon": "1y",
         "rankable": True, "avg_alpha": float(i)}
        for i, aid in enumerate(ids)
    ]


def _scores(ticker, actor_type, direction, actor_ids):
    return [
        {"ticker": ticker, "actor_type": actor_type, "actor_id": aid,
         "direction": direction}
        for aid in actor_ids
    ]


def _patch_docs(monkeypatch, perf, scores):
    """Patch the two `mongo_store.find_docs` reads, dispatching on COLLECTION.

    `smart_money_quality_line` imports mongo_store INSIDE the function, so the
    patch lands on `app.db.mongo_store` rather than on the alt_data_block
    attribute (which is what the old `get_db` patch tried, and missed).
    """
    def _find_docs(collection, *_a, **_k):
        if collection == "smart_money_performance":
            return perf
        if collection == "smart_money_trade_scores":
            return scores
        raise AssertionError(f"unexpected collection {collection!r}")

    monkeypatch.setattr("app.db.mongo_store.find_docs", _find_docs)


def _nvda_fixture():
    """11 buy-side funds at the 57th percentile, 2 sell-side congress at the 13th."""
    # All 11 funds share one alpha rank, so the median IS that percentile.
    fund_cohort = _cohort("fund", 15, 8, "fund-subject")
    # Ten more funds tied at the subject's alpha, appended so the cohort stays
    # 15 distinct ALPHAS while 11 actors sit on the same rank.
    fund_cohort = fund_cohort[:8] + [
        {"actor_type": "fund", "actor_id": f"fund-buyer-{i}", "horizon": "1y",
         "rankable": True, "avg_alpha": 8.0}
        for i in range(11)
    ] + fund_cohort[9:]
    congress_cohort = _cohort("congress", 164, 21, "congress-subject")
    congress_cohort = congress_cohort[:21] + [
        {"actor_type": "congress", "actor_id": f"congress-seller-{i}",
         "horizon": "1y", "rankable": True, "avg_alpha": 21.0}
        for i in range(2)
    ] + congress_cohort[22:]

    scores = (
        _scores("NVDA", "fund", "buy", [f"fund-buyer-{i}" for i in range(11)])
        + _scores("NVDA", "congress", "sell",
                  [f"congress-seller-{i}" for i in range(2)])
    )
    return fund_cohort + congress_cohort, scores


def test_quality_line_reports_percentile_not_alpha(monkeypatch):
    perf, scores = _nvda_fixture()
    _patch_docs(monkeypatch, perf, scores)

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
    perf, scores = _nvda_fixture()
    _patch_docs(monkeypatch, perf, scores)

    line = smart_money_quality_line("NVDA")
    assert "pp" not in line, "a percentage-point alpha figure leaked into the block"
    assert "hand-picked" in line, "the selection-bias caveat must travel with it"


def test_quiet_ticker_returns_empty(monkeypatch):
    """A full cohort but no trades in this name is silence, not a zero score."""
    perf, _ = _nvda_fixture()
    _patch_docs(monkeypatch, perf, [])
    assert smart_money_quality_line("NOBODYTRADESTHIS") == ""


def test_blank_ticker_never_queries(monkeypatch):
    def _explode(*_a, **_k):
        raise AssertionError("must not query for a blank ticker")

    monkeypatch.setattr("app.db.mongo_store.find_docs", _explode)
    assert smart_money_quality_line("") == ""
    assert smart_money_quality_line(None) == ""


def test_query_failure_is_fail_open(monkeypatch):
    """Every line in this block degrades to "" — never a pipeline error."""
    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.db.mongo_store.find_docs", _boom)
    assert smart_money_quality_line("NVDA") == ""


@pytest.mark.parametrize("n,expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
    (11, "11th"), (12, "12th"), (13, "13th"),
    (21, "21st"), (52, "52nd"), (73, "73rd"), (57, "57th"), (100, "100th"),
])
def test_ordinal(n, expected):
    assert _ordinal(n) == expected
