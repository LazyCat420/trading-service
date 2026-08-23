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


# ── the debate's STRUCTURE reaches the deciders (ch.90 fix A3) ───────────────
# Audit 2026-08-23: bull claims[], bear rebuttals/counter_evidence/
# independent_risks and the judge's verified/unverified lists were NEVER
# rendered into the Board's view — only summary prose, and
# bear.independent_risks was absent from what the Board read on 50% of desks.

def _desk_with_structured_debate():
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-test")
    desk.append_artifact("bull_argument", {
        "summary": "Bull prose.",
        "confidence": 70,
        "claims": [
            {"claim": "Revenue accelerating 3 quarters straight",
             "evidence_source": "10-Q", "strength": "strong"},
            {"claim": "Margins expanding", "evidence_source": "10-K",
             "strength": "moderate"},
        ],
    })
    desk.append_artifact("bear_rebuttal", {
        "summary": "Bear prose.",
        "confidence": 65,
        "rebuttals": [
            {"bull_claim_addressed": "Revenue accelerating 3 quarters straight",
             "rebuttal": "Acceleration is an acquisition artifact",
             "counter_evidence": "Organic growth 2% per the 10-Q segment table"},
        ],
        "independent_risks": [
            "Customer concentration: top-3 are 61% of revenue",
            "Convertible notes mature in March",
        ],
    })
    desk.append_artifact("debate_judge", {
        "summary": "Judge prose.",
        "winner": "bear",
        "final_confidence": 62,
        "verified_bull_claims": ["Margins expanding"],
        "unverified_bull_claims": ["Revenue accelerating 3 quarters straight"],
        "verified_bear_claims": ["Organic growth 2%"],
        "unverified_bear_claims": [],
        "proposition_verdicts": [
            {"proposition": "Is the growth organic?", "verdict": "NO",
             "why": "segment table shows 2%"},
        ],
        "unanswered_bear_risks": ["Convertible dilution"],
    })
    return desk


def test_bear_independent_risks_reach_the_board_view():
    ctx = _desk_with_structured_debate().get_compressed_context(include_debate=True)
    assert "Customer concentration: top-3 are 61% of revenue" in ctx
    assert "Convertible notes mature in March" in ctx


def test_bull_claims_and_judge_lists_render_verbatim():
    ctx = _desk_with_structured_debate().get_compressed_context(include_debate=True)
    assert "Revenue accelerating 3 quarters straight" in ctx
    assert "Acceleration is an acquisition artifact" in ctx
    assert "Organic growth 2% per the 10-Q segment table" in ctx
    assert "UNVERIFIED bull claims" in ctx
    assert "Is the growth organic?" in ctx


def test_structure_is_bounded():
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-test")
    desk.append_artifact("bull_argument", {
        "summary": "s", "confidence": 50,
        "claims": [{"claim": "C" * 1000, "evidence_source": "E" * 500,
                    "strength": "strong"} for _ in range(50)],
    })
    block = desk._debate_structure_block(include_verdicts=True)
    assert block.count("- ") <= 5
    assert len(block) < 3500


def test_structure_survives_a_saturated_desk():
    """The anti-recreate-the-defect pin: on a desk fat enough to trip the
    research tail-cut, the structure block must still be present — it rides
    the protected verdict channel."""
    desk = _desk_with_structured_debate()
    desk.append_artifact("desk_note", {
        "summary": "A" * 12000, "key_findings": [], "data_gaps": [],
        "confidence": 50,
    })
    desk.append_artifact("final_decision", {
        "action": "HOLD", "confidence": 60, "reasoning": "R" * 500,
    })
    ctx = desk.get_compressed_context(include_debate=True)
    assert "research context TRUNCATED" in ctx, "the cut must actually fire"
    assert "Customer concentration: top-3 are 61% of revenue" in ctx
    assert "## Board of Directors Verdict" in ctx


def test_bare_string_items_from_old_rows_render():
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-test")
    desk.append_artifact("bull_argument", {
        "summary": "s", "confidence": 50, "claims": ["a bare string claim"],
    })
    assert "a bare string claim" in desk._debate_structure_block(True)


def test_shadow_mode_hides_judge_structure_keeps_bull_bear():
    desk = _desk_with_structured_debate()
    block = desk._debate_structure_block(include_verdicts=False)
    assert "Customer concentration: top-3 are 61% of revenue" in block
    assert "Judge" not in block
