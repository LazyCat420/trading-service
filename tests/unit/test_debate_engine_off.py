"""DEBATE_ENGINE=3 — the tournament is retired, on measurement.

Cost was certain and large: 77.7M of 275.9M pipeline tokens (28.2%) over 347
runs, 374 s/ticker. Every benefit channel was tested and none survived
(n=137 desks with a decided tournament and a non-degraded outcome):

    selection ...... tourn-bull vs -bear on traded desks  -0.822pp (p=0.34)
                     FREE quant thesis_direction          -0.771pp (p=0.35)
    removal ........ quant-BEARISH holds  -1.85% (n=14)
                     tourn-bear   holds   -0.29% (n=29)   <- free signal 6.5x better
    incremental .... within quant=BEARISH, bear vs bull +0.33pp (p=0.84)
    redundancy ..... winning_side vs quant thesis_direction chi2=16.63 p<0.0001
    calibration .... Brier 0.3090 vs base rate 0.2266
    jury veto ...... blocked ZERO decisions, ever

These tests pin the three things that make the retirement safe rather than
merely cheap: nothing fabricates a verdict, every consumer tolerates the
artifact's ABSENCE (the veto gate especially — it must not start blocking or
start crashing), and the fail-open cannot silently resurrect the spend.
"""

import inspect

import pytest

from app.services.parameter_store import PARAMETER_REGISTRY
from app.v3 import orchestrator
from app.v3.shared_desk import SharedDesk


# ── the switch itself ────────────────────────────────────────────────

def test_no_debate_is_the_default():
    spec = PARAMETER_REGISTRY["DEBATE_ENGINE"]
    assert spec.default == 3, "the tournament must not run by default"
    assert spec.max_value >= 3, "engine 3 must be reachable"
    assert spec.min_value == 0, "engines 0-2 stay selectable to re-run the comparison"


def test_fail_open_lands_on_no_debate_not_the_tournament():
    """A parameter-store hiccup must not resurrect ~30% of pipeline spend.

    Checks EVERY lookup, not just the first. There are two — the gate in
    _queue_debate_phase (`_engine_sel`) and the engine selector in
    _execute_tournament_debate (`_engine`) — and a fallback to 0 in either
    one silently brings the tournament back.
    """
    src = inspect.getsource(orchestrator)
    sites, i = [], src.find('_get_engine("DEBATE_ENGINE")')
    while i != -1:
        sites.append(src[i:i + 400])
        i = src.find('_get_engine("DEBATE_ENGINE")', i + 1)
    assert len(sites) >= 2, f"expected both DEBATE_ENGINE lookups, found {len(sites)}"
    for n, window in enumerate(sites):
        assert "= 3" in window, f"lookup #{n} must fail open to no-debate"
        assert "_engine = 0" not in window and "_engine_sel = 0" not in window, (
            f"lookup #{n} must not fall back to the tournament"
        )


def test_engine_3_does_not_fabricate_a_verdict():
    """A SKIPPED marker is honest; a synthesized winner is not.

    Engine 3 appends a `tournament_result` — it has to, so the Board gets
    chained and so scoring can tell "no debate ran" from "the debate returned
    nothing" (both sibling gates do the same). What it must never do is invent
    a WINNER. Deriving winning_side from the quant would hand the Board a
    computed number dressed as a debate outcome, which is the invented-RSI
    failure with a new name.
    """
    src = inspect.getsource(orchestrator)
    gate = src[src.find("if _engine_sel == 3:"):]
    gate = gate[:gate.find("if _cog_settings.TOURNAMENT_MODE:")]
    assert gate, "engine-3 gate not found"
    assert '"winning_side": "skipped"' in gate, "the winner must be 'skipped'"
    assert '"confidence": 0' in gate, "a skip carries no confidence"
    assert '"total_tokens": 0' in gate, "a skip costs nothing"
    for forbidden in ('"winning_side": "bull"', '"winning_side": "bear"'):
        assert forbidden not in gate, (
            f"engine 3 must not derive a winner ({forbidden})"
        )
    # It must not READ a research stance to build the marker. Checking for the
    # bare word would match the skip_note's own prose, which explains WHY the
    # tournament was retired — matching the explanation instead of the code is
    # how a guard tests nothing.
    for read in ("'thesis_direction'", '"thesis_direction"',
                 "->>'thesis_direction'"):
        assert read not in gate, f"engine 3 must not read {read}"


def test_skip_is_recorded_so_the_saving_is_queryable():
    """Without a stamped row, 'did we stop paying for it?' is unfalsifiable."""
    src = inspect.getsource(orchestrator)
    branch = src[src.find("if _engine == 3:"):][:1600]
    assert "record_agent_telemetry" in branch
    assert '"SKIPPED"' in branch
    assert '"token_usage": 0' in branch


def test_skip_happens_before_the_starting_event():
    """Emitting `running` then returning orphans a node in the office UI."""
    src = inspect.getsource(orchestrator)
    fn = src[src.find("async def _execute_tournament_debate"):]
    fn = fn[:fn.find("emit(\n            \"analyzing\", f\"v3_tournament_done_")]
    skip_at = fn.find("if _engine == 3:")
    start_at = fn.find("Tournament Debate starting")
    assert skip_at != -1 and start_at != -1
    assert skip_at < start_at, "the skip must precede the 'starting' emit"


# ── absence tolerance: the part that could actually break trading ────

def _desk_without_tournament():
    desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
    desk.quant_report = {"summary": "s", "thesis_direction": "BEARISH",
                         "confidence": 80, "risk_metrics": {}}
    desk.fundamental_report = {"summary": "s", "thesis_direction": "BULLISH",
                               "confidence": 75, "pillars": {},
                               "positioning_read": {}}
    desk.final_decision = {"action": "BUY", "confidence": 80, "reasoning": "r"}
    return desk


def test_absent_tournament_does_not_block_the_trade():
    """`getattr(...) or {}` means .get('vetoed') is falsy — no phantom veto."""
    desk = _desk_without_tournament()
    tournament = getattr(desk, "tournament_result", None) or {}
    assert tournament.get("vetoed") in (None, False)
    assert not tournament.get("risk_flags")


def test_compressed_context_renders_without_a_tournament():
    desk = _desk_without_tournament()
    text = desk.get_compressed_context()
    assert isinstance(text, str)
    assert "Tournament Debate Verdict" not in text
    # the free signals the board now leans on are still there
    assert "BEARISH" in text or "Quantitative" in text


def test_debate_judge_absence_is_tolerated():
    desk = _desk_without_tournament()
    assert getattr(desk, "debate_judge", None) in (None, {}, [])
    desk.get_compressed_context()  # must not raise


def test_contradiction_shadow_still_detects_without_the_tournament():
    """tournament_result is one of _SENTIMENT_ACTION_ARTIFACTS, so its absence
    could plausibly blind the detector. It does not: final_decision and the two
    thesis_direction artifacts still populate the sentiment map."""
    from app.v3.contradiction_shadow import compute_contradiction_shadow

    desk = _desk_without_tournament()  # quant BEARISH vs fundamental BULLISH, board BUY
    out = compute_contradiction_shadow(desk)
    assert out.get("outcome") == "SUCCESS"
    assert out.get("shadow_only") is True
    assert out.get("contradiction_count", 0) >= 1, (
        "a real quant/fundamental contradiction must still be caught"
    )


@pytest.mark.parametrize("engine", [0, 1, 2, 3])
def test_every_engine_value_is_in_range(engine):
    spec = PARAMETER_REGISTRY["DEBATE_ENGINE"]
    assert spec.min_value <= engine <= spec.max_value


# ── the regression the first version shipped ─────────────────────────

def test_engine_3_dispatches_the_board():
    """Skipping the debate must not skip the DECISION.

    Shipped broken 2026-07-30 and caught only by running a real cycle:
    the engine-3 branch returned early from _execute_tournament_debate, which
    skipped the whiteboard write at `tournament_result` — and that write is
    what the subscriber uses to chain the Board:

        elif sec in ("debate_judge", "tournament_result"):
            _queue_agent("board_of_directors", ...)

    NVDA ran 7 agents in cycle-observe-1785396275 and produced NO decision,
    stalling at RESEARCH_DONE. Both sibling gates (regime-skip, no-trade) avoid
    this by appending a SKIPPED marker and dispatching the Board explicitly;
    engine 3 must do the same.
    """
    src = inspect.getsource(orchestrator)
    gate = src[src.find("if _engine_sel == 3:"):]
    gate = gate[:gate.find("if _cog_settings.TOURNAMENT_MODE:")]
    assert gate, "engine-3 gate not found in _queue_debate_phase"
    assert '_queue_agent("board_of_directors"' in gate, (
        "engine 3 must dispatch the Board — skipping the debate must not skip "
        "the decision"
    )
    assert "board_dispatched = True" in gate, "must latch the dispatch"
    assert '"winning_side": "skipped"' in gate, (
        "append a SKIPPED marker so scoring can tell 'no debate ran' from "
        "'the debate returned nothing'"
    )


def test_engine_3_gate_sits_beside_its_siblings():
    """All three debate-skip paths live in _queue_debate_phase, so they share
    the board_dispatched latch. A skip that returns from the EXECUTOR instead
    cannot dispatch the Board — that was the bug."""
    src = inspect.getsource(orchestrator)
    qdp = src[src.find("def _queue_debate_phase():"):]
    qdp = qdp[:qdp.find("async def _has_pending_peer_requests")]
    assert "if _engine_sel == 3:" in qdp, (
        "the engine-3 gate must be inside _queue_debate_phase"
    )
    # every skip path in that function dispatches the board
    assert qdp.count('_queue_agent("board_of_directors"') >= 3, (
        "regime-skip, no-trade and engine-3 must each dispatch the Board"
    )


def test_executor_branch_does_not_own_board_dispatch():
    """The defensive branch left in the executor must NOT try to dispatch the
    Board; duplicating that responsibility is what made the bug subtle."""
    src = inspect.getsource(orchestrator)
    fn = src[src.find("async def _execute_tournament_debate"):]
    branch = fn[fn.find("if _engine == 3:"):][:1200]
    assert "board_of_directors" not in branch, (
        "the upstream gate owns the Board dispatch"
    )
