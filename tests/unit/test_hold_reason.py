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


# ── The call sites ───────────────────────────────────────────────────────
#
# The test above passed from the day it was written, and the delta route STILL
# never produced a label: `classify_hold` handled the shape, but the only call
# site sat ~1,600 lines below the delta tier's `return result`. Measured
# 2026-08-08 — 1 of the 3 HOLDs since the deploy carried a label, and 52
# `v3_delta_done_*` decisions lifetime could never have carried one.
#
# **A guarded callee does not protect its call site.** Everything below is
# about the call sites, because nothing above could see them.


def _pipeline_exits():
    """Every `return result` in `run_v3_pipeline`, with its preceding source."""
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "app" / "v3" / "orchestrator.py"
    src = path.read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "run_v3_pipeline"
    )
    returns = sorted(
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.Name)
        and n.value.id == "result"
    )
    lines = src.splitlines()
    out = []
    prev = fn.lineno
    for lineno in returns:
        out.append((lineno, "\n".join(lines[prev:lineno])))
        prev = lineno
    return out


def test_every_decision_exit_is_accounted_for():
    """Three exits today: glance, delta, full panel. A fourth appearing without
    a decision here is the thing that silently loses coverage."""
    assert len(_pipeline_exits()) == 3, (
        "run_v3_pipeline gained or lost a `return result`. Decide explicitly "
        "whether the new exit carries a WATCH/AVOID label and record it here."
    )


def test_the_delta_and_full_panel_exits_attach_the_label():
    """The two exits where an agent actually decided."""
    exits = _pipeline_exits()
    labelled = [ln for ln, body in exits if "_attach_hold_reason(" in body]
    assert len(labelled) == 2, (
        f"expected the delta and full-panel exits to attach a hold reason, "
        f"got {len(labelled)} of {len(exits)} at lines {labelled}"
    )


def test_the_glance_exit_deliberately_does_not():
    """A glance skip is an age/news heuristic that ran before any agent, so
    `classify_hold` would return WATCH — 'thesis constructive, not entering
    yet' — a claim nobody made. Same error the 07-25 audit fixed by stamping
    TRIAGE_SKIP, and it would pollute the low-band concentration check that is
    the only test of whether the split works."""
    first_exit_line, first_exit_body = _pipeline_exits()[0]

    assert "v3_glance" in first_exit_body, "the first exit is no longer the glance tier"
    assert "_attach_hold_reason(" not in first_exit_body, (
        "the glance tier must not carry a WATCH/AVOID label"
    )
    assert "DELIBERATELY NO" in first_exit_body, (
        "the omission must be stated, not left to look like the oversight it "
        "used to be"
    )


def test_the_helper_attaches_and_emits():
    """Behaviour, not structure: the helper puts the label on the result and
    emits it, and a classification failure never costs the decision."""
    from app.v3.orchestrator import _attach_hold_reason

    emitted = []
    result = {"action": "HOLD"}
    _attach_hold_reason(
        result,
        desk=_desk(trade_decision={"thesis_direction": "BEARISH"}),
        ticker="NVDA",
        emit=lambda *a, **kw: emitted.append((a, kw)),
    )
    assert result["hold_reason"] == AVOID
    assert emitted and emitted[0][0][1] == "v3_hold_reason_NVDA"
    assert emitted[0][1]["data"]["hold_reason"] == AVOID


def test_a_broken_classifier_never_costs_the_decision():
    """Non-fatal by construction — a label must never abort a ticker."""
    from unittest.mock import patch

    from app.v3 import orchestrator

    result = {"action": "HOLD"}
    with patch("app.v3.hold_reason.classify_hold", side_effect=RuntimeError("boom")):
        orchestrator._attach_hold_reason(
            result, desk=_desk(), ticker="NVDA", emit=lambda *a, **kw: None,
        )
    assert "hold_reason" not in result
    assert result["action"] == "HOLD"


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


# ── The substitute axis (reworked 2026-08-08) ────────────────────────────
#
# AVOID now means "the bear named something better", not "the desk feels
# negative". The three signals still travel in `signals` on every call: they
# are the ONLY axis on routes where no bear runs (the delta tier is one
# agent), and keeping both measurable is what makes this reversible.

def _with_substitute(status, ticker=None, **kw):
    from app.v3.substitute import _META_KEY

    meta = {_META_KEY: {"status": status, "ticker": ticker, "pool_size": 3}}
    meta.update(kw.pop("cycle_metadata", {}))
    return _desk(cycle_metadata=meta, **kw)


def test_a_named_substitute_is_avoid():
    from app.v3.substitute import NAMED

    result = classify_hold(_with_substitute(NAMED, "PLTR"), "HOLD")
    assert result["hold_reason"] == AVOID
    assert result["basis"] == "substitute:named"
    assert result["substitute_ticker"] == "PLTR"


def test_a_declined_substitute_is_watch():
    from app.v3.substitute import DECLINED

    result = classify_hold(_with_substitute(DECLINED), "HOLD")
    assert result["hold_reason"] == WATCH
    assert result["basis"] == "substitute:declined"
    assert result["substitute_ticker"] is None


def test_the_substitute_outranks_the_signals_in_BOTH_directions():
    """The whole point of the rework. A bear that won the debate but found
    nothing better is a WATCH — the desk is negative and has no better use for
    the capital — and a bear that named one is an AVOID with no signal at all.
    A test checking only one direction would pass on code that ignored the
    substitute entirely."""
    from app.v3.substitute import DECLINED, NAMED

    declined = classify_hold(
        _with_substitute(
            DECLINED,
            cycle_metadata={"decision_score": {"band": "AVOID"}},
            debate_judge={"winning_side": "bear"},
            final_decision={"thesis_direction": "BEARISH"},
        ),
        "HOLD",
    )
    assert declined["hold_reason"] == WATCH
    assert len(declined["signals"]) == 3, "the old axis must stay measurable"

    named = classify_hold(_with_substitute(NAMED, "ABNB"), "HOLD")
    assert named["hold_reason"] == AVOID
    assert named["signals"] == [], "AVOID here is the substitute, not a signal"


@pytest.mark.parametrize("status", ["OFF_POOL", "UNANSWERED", "NOT_ASKED"])
def test_engagement_failures_fall_back_to_the_signals(status):
    """A name the desk cannot price, an ignored question, and an absent pool
    are not answers. Labelling from them would read a broken response as a
    considered one."""
    assert classify_hold(_with_substitute(status), "HOLD")["hold_reason"] == WATCH
    result = classify_hold(
        _with_substitute(status, debate_judge={"winning_side": "bear"}), "HOLD"
    )
    assert result["hold_reason"] == AVOID
    assert result["basis"] == "negative_signal"


def test_an_unexercised_wake_is_distinguishable_from_a_declining_bear():
    """Five of the last six cycles were Watch Desk wakes, which have no
    candidate pool at all. If NOT_ASKED and DECLINED both read as a bare WATCH,
    an unexercised feature looks identical to a working one."""
    from app.v3.substitute import DECLINED, NOT_ASKED

    asked = classify_hold(_with_substitute(DECLINED), "HOLD")
    never = classify_hold(_with_substitute(NOT_ASKED), "HOLD")
    assert asked["hold_reason"] == never["hold_reason"] == WATCH
    assert asked["substitute_status"] != never["substitute_status"]


def test_a_route_with_no_bear_still_classifies():
    """The delta tier is one agent and never runs a bear, so the substitute
    record is absent entirely — not a status, no key at all."""
    result = classify_hold(_desk(trade_decision={"thesis_direction": "SELL"}), "HOLD")
    assert result["hold_reason"] == AVOID
    assert result["basis"] == "negative_signal"
    assert result["substitute_status"] is None


@pytest.mark.parametrize("junk", ["not-a-dict", 42, [], None, {}, {"status": None}])
def test_a_malformed_substitute_record_does_not_raise(junk):
    from app.v3.substitute import _META_KEY

    desk = _desk(cycle_metadata={_META_KEY: junk})
    assert classify_hold(desk, "HOLD")["hold_reason"] == WATCH


def test_the_substitute_reaches_the_result_not_just_the_label():
    """An AVOID whose named alternative is only reachable by re-reading the
    bear's artifact is an AVOID nothing downstream can act on."""
    from app.v3.orchestrator import _attach_hold_reason
    from app.v3.substitute import NAMED

    result = {"action": "HOLD"}
    _attach_hold_reason(
        result, desk=_with_substitute(NAMED, "PLTR"), ticker="TSLA",
        emit=lambda *a, **kw: None,
    )
    assert result["hold_reason"] == AVOID
    assert result["hold_substitute"] == "PLTR"
    assert result["hold_reason_basis"] == "substitute:named"
