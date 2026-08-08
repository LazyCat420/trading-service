"""Tests for the WATCH/AVOID split.

Every test calls `classify_hold` itself. None of them re-implement the branch
logic and assert against the copy — that shape let a blocked trade read as a
kept one for weeks, because production was free to diverge from the test's
private copy of the rule.
"""

from types import SimpleNamespace

import pytest

from app.v3.hold_reason import AVOID, WATCH, classify_hold


def _desk(**kw):
    """A desk carrying only the artifacts a test cares about."""
    base = dict(
        debate_judge=None,
        final_decision=None,
        trade_decision=None,
        decision_synthesis=None,
        cycle_metadata={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── The action gate ──────────────────────────────────────────────────────

@pytest.mark.parametrize("action", ["BUY", "SELL", "buy", None, "", "UNKNOWN"])
def test_only_hold_is_classified(action):
    """BUY and SELL are executable and must not get a sub-label.

    Returning a default for them would put a meaningless value in a column
    that later reads as data.
    """
    assert classify_hold(_desk(), action) is None


@pytest.mark.parametrize("action", ["HOLD", "hold", " Hold "])
def test_hold_is_recognised_regardless_of_casing(action):
    result = classify_hold(_desk(), action)
    assert result is not None
    assert result["hold_reason"] in (WATCH, AVOID)


# ── The three negative signals ───────────────────────────────────────────

def test_bear_winning_the_debate_is_avoid():
    """The case the all-HOLD finding is actually about: on a book that cannot
    short, a bear verdict has no executable form and collapses to HOLD."""
    result = classify_hold(_desk(debate_judge={"winning_side": "bear"}), "HOLD")
    assert result["hold_reason"] == AVOID
    assert "debate:bear_won" in result["signals"]


def test_winner_key_is_honoured_as_well_as_winning_side():
    """The judge records the winner under either key; reading only one would
    silently classify half the desks as WATCH."""
    result = classify_hold(_desk(debate_judge={"winner": "BEAR"}), "HOLD")
    assert result["hold_reason"] == AVOID


def test_bull_winning_the_debate_is_watch():
    result = classify_hold(_desk(debate_judge={"winning_side": "bull"}), "HOLD")
    assert result["hold_reason"] == WATCH
    assert result["signals"] == []


def test_baseline_avoid_band_is_avoid():
    """The deterministic verdict owes nothing to any model and is present even
    when every agent failed."""
    desk = _desk(cycle_metadata={"decision_score": {"band": "AVOID"}})
    result = classify_hold(desk, "HOLD")
    assert result["hold_reason"] == AVOID
    assert "baseline:avoid_band" in result["signals"]


def test_other_bands_are_not_avoid():
    for band in ("NEUTRAL", "CANDIDATE", "STRONG_CANDIDATE", "NOT_SCOREABLE"):
        desk = _desk(cycle_metadata={"decision_score": {"band": band}})
        assert classify_hold(desk, "HOLD")["hold_reason"] == WATCH, band


def test_bearish_final_decision_is_avoid():
    desk = _desk(final_decision={"thesis_direction": "BEARISH"})
    result = classify_hold(desk, "HOLD")
    assert result["hold_reason"] == AVOID
    assert "final_decision:bearish" in result["signals"]


def test_delta_tier_writes_trade_decision_directly():
    """The delta tier writes `trade_decision` without the synthesizer running,
    so reading only `final_decision` would miss every delta re-look."""
    desk = _desk(trade_decision={"thesis_direction": "SELL"})
    assert classify_hold(desk, "HOLD")["hold_reason"] == AVOID


# ── Composition and the default ──────────────────────────────────────────

def test_signals_accumulate():
    desk = _desk(
        debate_judge={"winning_side": "bear"},
        cycle_metadata={"decision_score": {"band": "AVOID"}},
        final_decision={"thesis_direction": "BEARISH"},
    )
    result = classify_hold(desk, "HOLD")
    assert result["hold_reason"] == AVOID
    assert len(result["signals"]) == 3


def test_empty_desk_is_watch_but_says_why():
    """A desk whose agents all failed produces WATCH, and that must NOT read
    as 'the desk likes this name'. The basis field states it outright."""
    result = classify_hold(_desk(), "HOLD")
    assert result["hold_reason"] == WATCH
    assert result["signals"] == []
    assert result["basis"] == "no_negative_signal"


def test_avoid_states_its_basis_too():
    result = classify_hold(_desk(debate_judge={"winner": "bear"}), "HOLD")
    assert result["basis"] == "negative_signal"


# ── Robustness: a label must never cost a decision ───────────────────────

@pytest.mark.parametrize("junk", [None, "not-a-dict", 42, [], {"winning_side": None}])
def test_malformed_artifacts_do_not_raise(junk):
    desk = _desk(debate_judge=junk, final_decision=junk, cycle_metadata={})
    result = classify_hold(desk, "HOLD")
    assert result["hold_reason"] in (WATCH, AVOID)


def test_missing_cycle_metadata_does_not_raise():
    desk = SimpleNamespace(debate_judge=None, final_decision=None,
                           trade_decision=None, decision_synthesis=None)
    assert classify_hold(desk, "HOLD")["hold_reason"] == WATCH


def test_bull_defense_is_not_read_as_a_bear_win():
    """`bull_defense` contains the substring 'bull', not 'bear' — but a naive
    substring check on the wrong field could still trip. Pin the real one."""
    desk = _desk(debate_judge={"winning_side": "bull_defense"})
    assert classify_hold(desk, "HOLD")["hold_reason"] == WATCH
