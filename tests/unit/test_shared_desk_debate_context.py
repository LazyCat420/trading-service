"""Regression tests for the debate context handed to the Board of Directors.

Bug history (2026-07-14): the tournament wrote its verdict with keys
winning_side/confidence but get_compressed_context read winner/final_confidence,
so every board prompt rendered "Debate Judge Verdict (Winner:  @ 0% confidence)"
regardless of the real outcome — the board never saw the debate result.
"""
from app.v3.shared_desk import SharedDesk


def _desk_with_tournament(vetoed=False):
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-test")
    desk.append_artifact("tournament_result", {
        "summary": "Bull momentum thesis survived backtest and jury.",
        "action": "BUY",
        "confidence": 80,
        "winning_side": "bull",
        "vetoed": vetoed,
    })
    desk.append_artifact("debate_judge", {
        "summary": "Bull momentum thesis survived backtest and jury.",
        "action": "BUY",
        "confidence": 80,
        "winning_side": "bull",
        "source": "tournament_debate",
    })
    return desk


def test_tournament_verdict_visible_in_debate_context():
    ctx = _desk_with_tournament().get_compressed_context(include_debate=True)
    assert "Tournament Debate Verdict" in ctx
    assert "BUY @ 80% confidence (winner: bull)" in ctx
    # The old bug rendered a blank winner at 0% — must never happen again
    assert "Winner:  @ 0%" not in ctx


def test_veto_is_labelled():
    ctx = _desk_with_tournament(vetoed=True).get_compressed_context(include_debate=True)
    assert "[JURY VETO]" in ctx


def test_tournament_judge_copy_not_duplicated():
    ctx = _desk_with_tournament().get_compressed_context(include_debate=True)
    # debate_judge is a copy of the tournament verdict — render it once
    assert ctx.count("Bull momentum thesis survived") == 1


def test_classic_judge_keys_still_render():
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-test")
    desk.append_artifact("debate_judge", {
        "summary": "Bear case wins on valuation.",
        "winner": "bear",
        "final_confidence": 65,
    })
    ctx = desk.get_compressed_context(include_debate=True)
    assert "Winner: bear @ 65% confidence" in ctx


def test_debate_context_excluded_when_not_requested():
    ctx = _desk_with_tournament().get_compressed_context(include_debate=False)
    assert "Tournament Debate Verdict" not in ctx


def test_extract_debate_result_tournament_mode():
    from app.v3.orchestrator import _extract_debate_result
    d = _extract_debate_result(_desk_with_tournament())
    assert d is not None, "d_result must be populated in tournament mode"
    assert d["action"] == "BUY"
    assert d["confidence"] == 80
    assert d["winning_side"] == "bull"
    assert d["original_thesis_status"] == "HELD"

    d_veto = _extract_debate_result(_desk_with_tournament(vetoed=True))
    assert d_veto["original_thesis_status"] == "VETOED"
