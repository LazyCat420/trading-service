"""A grounding red card must deduct in proportion to the miss.

`final_quality_score = 0 if red_cards else base_score` treated "faithfulness
0.69 against a 0.70 threshold" and "faithfulness 0.0" as the same verdict.

Measured over the seven days to 2026-09-04, 7 of 22 judged decisions carried a
red card and were zeroed while their judge_a_score was 3.5-4.5. That pulled the
7-day judge mean from 4.19 (83.8%) to 2.92 (58.3%), which in turn:
  - fired the "LLM-judge decision quality low: 58% (7d avg)" finding (the
    threshold is 0.60),
  - cost LLM Performance ~7.6 points (the judge term carries weight 0.3), and
  - tripped the Goodhart tripwire at 7/22 = 32% against a 10% rate.
Three dashboard indicators moved by one if-statement. At least three of the
seven card texts stated the claims WERE supported and red-carded anyway.

Every test here fails on the pre-fix rule, which returns 0 for all of them.
"""

import pytest

from app.cognition.evaluation.judge_agent import (
    FAITHFULNESS_THRESHOLD,
    RELEVANCY_THRESHOLD,
    apply_red_card_penalty,
    grounding_shortfall,
)


def test_a_near_miss_barely_moves_the_score():
    """0.69 against a 0.70 threshold is a doubt, not a disqualification."""
    shortfall = grounding_shortfall(FAITHFULNESS_THRESHOLD - 0.01, FAITHFULNESS_THRESHOLD)
    final, penalty = apply_red_card_penalty(4.5, [shortfall], True)
    assert penalty < 0.05
    assert final > 4.2, f"a hair under threshold cost {4.5 - final:.2f} points"


def test_a_bottomed_out_metric_still_takes_everything():
    """The extreme the old rule was written for must survive the change."""
    final, penalty = apply_red_card_penalty(4.5, [grounding_shortfall(0.0, FAITHFULNESS_THRESHOLD)], True)
    assert penalty == 1.0
    assert final == 0.0


def test_the_penalty_is_monotone_in_the_miss():
    """A worse metric can never score better. Pins the direction, not a value."""
    scores = [0.0, 0.1, 0.3, 0.5, 0.65, 0.69]
    finals = [
        apply_red_card_penalty(4.5, [grounding_shortfall(x, FAITHFULNESS_THRESHOLD)], True)[0]
        for x in scores
    ]
    assert finals == sorted(finals), finals
    assert len(set(finals)) > 1, "the band collapsed to a single value — this is the defect"


def test_half_way_down_costs_about_half():
    half = FAITHFULNESS_THRESHOLD / 2
    final, penalty = apply_red_card_penalty(4.0, [grounding_shortfall(half, FAITHFULNESS_THRESHOLD)], True)
    assert penalty == pytest.approx(0.5, abs=0.01)
    assert final == pytest.approx(2.0, abs=0.02)


def test_the_worst_card_governs_when_two_fire():
    worst = grounding_shortfall(0.1, FAITHFULNESS_THRESHOLD)
    mild = grounding_shortfall(RELEVANCY_THRESHOLD - 0.01, RELEVANCY_THRESHOLD)
    final, penalty = apply_red_card_penalty(4.0, [mild, worst], True)
    assert penalty == pytest.approx(worst)
    assert final < 1.0


def test_no_red_card_leaves_the_score_untouched():
    assert apply_red_card_penalty(4.38, [], False) == (4.38, 0.0)


def test_a_card_with_no_score_behind_it_keeps_the_full_penalty():
    """Conservative fallback: an unscored card must not become free."""
    assert apply_red_card_penalty(4.5, [], True) == (0.0, 1.0)


def test_the_measured_seven_day_window_recovers():
    """The seven zeroed rows, replayed at a plausible near-threshold miss.

    Not a claim about what those rows actually scored — the stored red cards
    carry prose, not a machine-readable metric. It pins the SHAPE: a window
    where every card is a near-miss must not read as 58%.
    """
    judge_a = [4.5, 2.75, 4.5, 4.5, 4.0, 4.0, 4.0, 3.5, 4.0, 4.0, 4.0,
               4.5, 4.5, 4.5, 4.5, 4.5, 4.0, 4.0, 4.0, 4.5, 4.38, 5.0]
    carded = {3, 6, 7, 8, 9, 10, 18}  # the 7 rows that were zeroed
    near_miss = grounding_shortfall(FAITHFULNESS_THRESHOLD - 0.05, FAITHFULNESS_THRESHOLD)
    finals = [
        apply_red_card_penalty(v, [near_miss], True)[0] if i in carded else v
        for i, v in enumerate(judge_a)
    ]
    mean_pct = sum(finals) / len(finals) / 5.0
    assert mean_pct > 0.60, f"still below the finding threshold: {mean_pct:.1%}"
    assert mean_pct < 0.90, "the penalty vanished entirely — that is the other failure"
