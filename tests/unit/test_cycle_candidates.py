"""The desk's first cross-ticker surface.

Measured 2026-08-08: `whiteboard_read` and `whiteboard_write` both take
`ticker` as a REQUIRED argument, so no agent could see any other name in the
cycle. On a long-only, one-position book that makes every bear thesis
unexecutable — 273 of 333 HOLDs in 30 days were the agent's own verdict before
any gate ran.

These tests cover the surface, not the behaviour it is meant to enable. What
they can see: the block is built from data that cannot race, it never claims
authority it has not earned, and the wiring reaches the agents it should and
no others.
"""

import pytest

from app.v3.cycle_candidates import (
    MAX_CANDIDATES,
    build_candidate_block,
    build_candidate_set,
    candidate_context,
)

SCORERS = [
    {"ticker": "pltr", "score": 91.2, "chg": 3.4, "rvol": 2.1},
    {"ticker": "ABNB", "score": 77.0, "chg": -1.2, "rvol": 1.1},
    {"ticker": "COIN", "score": 55.5, "chg": 0.4, "rvol": 0.9},
]
META = {"PLTR": {"sector": "Technology"}, "ABNB": {"sector": "Consumer Cyclical"}}


# ── the candidate set ────────────────────────────────────────────────────

def test_tickers_are_normalised_and_sectors_enriched():
    out = build_candidate_set(SCORERS, META)

    assert [c["ticker"] for c in out] == ["PLTR", "ABNB", "COIN"]
    assert out[0]["sector"] == "Technology"
    assert out[2]["sector"] is None, "a missing sector stays absent, not guessed"


def test_an_empty_pool_is_a_real_answer_not_a_failure():
    """A Watch Desk wake names its ticker explicitly and bypasses discovery, so
    there IS no candidate pool. That must not raise and must not be logged as
    an error — 5 of the last 6 cycles were exactly this shape."""
    assert build_candidate_set(None) == []
    assert build_candidate_set([]) == []
    assert build_candidate_block([]) == ""
    assert build_candidate_block(None) == ""


def test_junk_rows_are_dropped_rather_than_rendered():
    out = build_candidate_set([{"no_ticker": 1}, "notadict", {"ticker": "  "}, *SCORERS])
    assert [c["ticker"] for c in out] == ["PLTR", "ABNB", "COIN"]


def test_the_pool_is_capped():
    big = [{"ticker": f"T{i}", "score": i} for i in range(40)]
    assert len(build_candidate_set(big)) == MAX_CANDIDATES


# ── the rendered block ───────────────────────────────────────────────────

def test_the_analysed_name_is_excluded_from_its_own_alternatives():
    """The agent already has a full score block for its own name; listing it
    again invites a comparison between a detailed read and a one-line summary
    of the same thing."""
    block = build_candidate_block(build_candidate_set(SCORERS, META), self_ticker="PLTR")

    assert "PLTR" not in block
    assert "ABNB" in block and "COIN" in block


def test_a_block_with_nothing_left_to_show_is_empty_not_a_header():
    """Excluding the only candidate must not leave 'here are the alternatives'
    followed by nothing — a promise with no rows is worse than no block."""
    one = build_candidate_set([{"ticker": "NVDA", "score": 50}])
    assert build_candidate_block(one, self_ticker="NVDA") == ""


def test_the_block_calls_the_number_a_screen_and_not_a_verdict():
    """`top_scorers` carries a DISCOVERY score — relative volume, price change,
    SMA/RSI position, an untouched-ticker bonus. It is not the fundamentals
    composite, and the block must not borrow that authority. The measured risk
    of injecting a number is that the agent copies it: the HRP target weight
    was once read as an order size."""
    block = build_candidate_block(build_candidate_set(SCORERS, META))

    low = block.lower()
    assert "discovery score" in low
    assert "not a fundamental verdict" in low
    assert "no agent has read these names" in low
    assert "composite" not in low, (
        "calling it a composite claims the fundamentals score, which is not "
        "what this is and does not exist for other tickers at this point"
    )


def test_the_framing_permits_declining_to_name_an_alternative():
    """'Name a better one' read as 'always name a better one' would invent a
    preference the agent does not hold — the same defect as the HOLD it
    replaces, wearing a different label."""
    block = build_candidate_block(build_candidate_set(SCORERS, META))
    assert "none of them is better" in block.lower()


def test_missing_numbers_render_as_a_dash_not_a_crash():
    block = build_candidate_block([{"ticker": "XYZ", "score": None, "chg": None,
                                    "rvol": None, "sector": None}])
    assert "XYZ" in block and "—" in block


# ── the wiring ───────────────────────────────────────────────────────────

def test_candidate_context_reads_the_desk_and_never_raises():
    from types import SimpleNamespace

    assert candidate_context(SimpleNamespace(cycle_metadata={})) == ""
    assert candidate_context(SimpleNamespace()) == ""
    assert candidate_context(None) == ""
    assert candidate_context(
        SimpleNamespace(cycle_metadata={"cycle_candidates_context": "BLOCK"})
    ) == "BLOCK"


def test_the_pipeline_never_passes_a_possibly_unbound_candidate_list():
    """`top_scorers` is assigned in exactly ONE place, deep inside the
    dynamic-selection branch. Referencing it at the per-ticker call site would
    raise UnboundLocalError on every explicit-ticker run, and arguments
    evaluate at the CALL — outside any `try`. That exact shape once discarded
    the gatekeeper's nine selected tickers and ran a whole cycle on hardcoded
    AAPL (2026-08-06, cycle-v3-1786072624).

    So the name passed must be bound unconditionally, before any branch.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "app" / "services" / "pipeline_service.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_run_all_v3")

    # The unconditional declaration sits directly in the function body, not
    # nested inside an `if`/`try`/`for`.
    top_level = [
        t.id
        for stmt in fn.body
        if isinstance(stmt, (ast.Assign, ast.AnnAssign))
        for t in ([stmt.target] if isinstance(stmt, ast.AnnAssign) else stmt.targets)
        if isinstance(t, ast.Name)
    ]
    assert "cycle_candidates" in top_level, (
        "cycle_candidates must be bound at the top level of _run_all_v3, or an "
        "explicit-ticker run raises UnboundLocalError at the call site"
    )
    assert "top_scorers" not in top_level, (
        "top_scorers is conditionally bound — if this ever changes, revisit "
        "whether the indirection through cycle_candidates is still needed"
    )


@pytest.mark.parametrize("agent,expected", [
    ("v3_bear_agent", True),
    ("v3_board_of_directors", True),
    ("v3_decision_synthesizer", True),
    ("v3_bull_agent", False),
    ("v3_fundamental_analyst", False),
    ("v3_junior_analyst", False),
    ("v3_quant_analyst", False),
])
def test_only_the_bear_and_the_deciders_see_the_alternatives(agent, expected):
    """The bull and the analysts are meant to reach an independent read of THIS
    name. A bull handed a list of rivals is being invited to argue relatively
    when its job is to make the strongest case for one thing — the same
    reasoning that keeps the deterministic score block away from the analysts.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "app" / "v3" / "agent_runner.py").read_text()
    tree = ast.parse(src)

    gated = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body = ast.dump(node)
        if "cycle_candidates_context" not in body:
            continue
        for c in ast.walk(node.test):
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                gated.add(c.value)

    assert gated, "no agent gate found for cycle_candidates_context"
    assert (agent in gated) is expected
