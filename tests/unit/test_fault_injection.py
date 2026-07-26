"""Tier 1 — deterministic fault injection: bad data in, checker must fire.

A different question from the rest of the unit suite. Those tests ask "does
this work on good input"; these ask "does the checker actually CATCH bad
input" — which is the only question that matters for a guardrail. Every case
here is a real defect found in the 2026-07-25 audit, written so that it FAILS
against the unfixed code.

The governing rule these enforce, from DECISION_INTEGRITY_PLAN.md:

    A degraded result must never be representable as a confident one.
    This codebase's failure mode is laundering, not crashing.

Deliberately no LLM and no network: this tier runs in CI on every commit.
Tiers 2 (agentic, real tool calls) and 3 (chaos) live in
tests/integration/test_adversarial_agentic.py.
"""
from __future__ import annotations

import math

import pytest

from app.v3.orchestrator import _apply_policy_gates, _is_degraded_decision
from app.v3.shared_desk import SharedDesk, DecisionProvenance


def _desk(**overrides) -> SharedDesk:
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-fault-injection")
    desk.regime_classification = {"summary": "regime ok"}
    desk.final_decision = {
        "action": "BUY",
        "confidence": 80,
        "stop_loss": 100.0,
        "dynamic_trigger": {"type": "trailing_drop", "value": 0.1},
        "position_size_pct": 4.0,
    }
    for key, value in overrides.items():
        setattr(desk, key, value)
    return desk


# ── The degraded sentinel, driven END TO END ────────────────────────
#
# The P0 of the 2026-07-25 audit. The sentinel was tested in
# test_decision_provenance.py (is it WRITTEN?) and the gate in
# test_policy_gates.py (does it gate a string action?). Neither file ever
# passed the sentinel THROUGH the gate, so `None.upper()` shipped. These
# tests exist to make that specific gap unrepeatable.

_SENTINEL = {
    "summary": "Board did not produce a decision (outcome=AGENT_ERROR).",
    "action": None,
    "confidence": 0,
    "decision_provenance": DecisionProvenance.BOARD_DEGRADED_FALLBACK.value,
    "degrade_outcome": "AGENT_ERROR",
    "risk_flags": ["board_degraded_no_decision"],
}


def test_degraded_sentinel_does_not_crash_the_policy_gate():
    """`action: None` is the key PRESENT with a null value, so a
    `.get("action", "HOLD")` default never fires and `.upper()` raised."""
    desk = _desk(final_decision=dict(_SENTINEL), trade_decision=None)
    assert _apply_policy_gates(desk) == "HOLD_DEGRADED_NO_DECISION"


def test_degraded_sentinel_is_not_reported_as_a_no_signal_hold():
    """Both block the trade, but conflating them is the laundering this
    whole feature exists to stop: a degrade is the ABSENCE of a decision."""
    desk = _desk(final_decision=dict(_SENTINEL), trade_decision=None)
    assert _apply_policy_gates(desk) != "HOLD_NO_SIGNAL"


def test_a_real_hold_is_still_a_no_signal_hold():
    """The complement — narrowing must not relabel healthy decisions."""
    desk = _desk(final_decision={
        "action": "HOLD", "confidence": 65,
        "decision_provenance": DecisionProvenance.BOARD_REASONED.value,
    })
    assert _apply_policy_gates(desk) == "HOLD_NO_SIGNAL"


def test_a_deliberate_skip_is_not_a_degrade():
    """A Triage-Gate skip is a correct outcome, not a pipeline failure.
    Classifying it as degraded would relabel healthy skips across the
    dashboard, the memory store and policy_action."""
    for prov in ("triage_skip", "no_trade_gate_skip", "coerced_unshortable"):
        assert not _is_degraded_decision({"action": "HOLD", "decision_provenance": prov}), prov


def test_only_genuine_failures_count_as_degraded():
    for prov in ("board_degraded_fallback", "timeout_abort"):
        assert _is_degraded_decision({"action": "HOLD", "decision_provenance": prov}), prov


@pytest.mark.parametrize("junk", [None, "", 0, [], {}, float("nan")])
def test_is_degraded_never_raises_on_junk(junk):
    """A guardrail that crashes on malformed input is worse than none —
    it takes the whole ticker down with it."""
    assert _is_degraded_decision(junk) in (True, False)


# ── Malformed actions must never reach the executor ─────────────────

@pytest.mark.parametrize("bad_action", [None, "", "   ", 0, [], {}, 3.14, True])
def test_malformed_action_never_yields_an_execute(bad_action):
    """Whatever junk lands in `action`, the gate must resolve to a HOLD_*
    label. An EXECUTE_* on unparseable input is an order placed on noise."""
    desk = _desk(final_decision={"action": bad_action, "confidence": 90})
    assert _apply_policy_gates(desk).startswith("HOLD_")


@pytest.mark.parametrize("bad_conf", [None, float("nan"), "high", [], {}])
def test_malformed_confidence_never_passes_the_floor(bad_conf):
    """NaN survives a NOT NULL check and compares False against every
    threshold — the trap named in the audit's own traps section. A
    confidence that cannot be read is not a confidence that clears a floor."""
    desk = _desk(final_decision={"action": "BUY", "confidence": bad_conf})
    result = _apply_policy_gates(desk)
    assert result.startswith("HOLD_"), f"{bad_conf!r} produced {result}"


def test_nan_confidence_is_not_silently_treated_as_high():
    """Explicit because NaN is the one that has actually bitten: every
    comparison against it is False, so `conf < floor` reads as 'passed'."""
    desk = _desk(final_decision={"action": "BUY", "confidence": float("nan")})
    assert _apply_policy_gates(desk).startswith("HOLD_")
    assert math.isnan(desk.final_decision["confidence"])  # unchanged, just gated


# ── No shorting: the constraint that costs money ────────────────────

def test_unheld_sell_is_blocked():
    desk = _desk(final_decision={"action": "SELL", "confidence": 90})
    desk.cycle_metadata["held"] = False
    assert _apply_policy_gates(desk) == "HOLD_NO_POSITION"


def test_held_sell_is_allowed_through():
    """The failure mode of this guard that costs money is suppressing a
    REAL exit. An affirmative held=True must survive the gate."""
    desk = _desk(final_decision={"action": "SELL", "confidence": 90})
    desk.cycle_metadata["held"] = True
    assert _apply_policy_gates(desk) != "HOLD_NO_POSITION"


def test_unknown_holdings_does_not_coerce_a_sell():
    """held=None means the portfolio lookup failed, NOT 'not held'.
    Coercing on unknown would convert real exits into HOLDs — exactly what
    the broken bot_id did on 2026-07-24 when it read False for everything."""
    desk = _desk(final_decision={"action": "SELL", "confidence": 90})
    desk.cycle_metadata["held"] = None
    # Either it resolves live or it falls through to the executor's own
    # backstop; what it must NOT do is treat unknown as an affirmative no.
    assert _apply_policy_gates(desk) in (
        "EXECUTE_SELL", "HOLD_NO_POSITION", "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE",
    )


# ── Factors must refuse to fabricate ────────────────────────────────

def test_degenerate_cross_section_yields_no_factor_not_a_zero():
    """A zero-filled factor reads as 'perfectly average', which is a
    fabrication, not a measurement. The module docstring forbids it; the
    `sd <= 0` branch did it anyway."""
    from app.quant.factors import _z_score

    identical = {f"T{i}": 5.0 for i in range(8)}  # zero variance
    out = _z_score(identical)
    assert out == {} or all(v is None for v in out.values()), (
        f"zero-variance cross-section fabricated z-scores: {out}"
    )


def test_thin_cross_section_yields_no_factor():
    from app.quant.factors import _z_score, MIN_CROSS_SECTION

    thin = {f"T{i}": float(i) for i in range(MIN_CROSS_SECTION - 1)}
    assert _z_score(thin) == {}


def test_z_score_is_computed_on_a_real_cross_section():
    """The complement — the guard must not be so tight it refuses valid work."""
    from app.quant.factors import _z_score, MIN_CROSS_SECTION

    real = {f"T{i}": float(i) for i in range(MIN_CROSS_SECTION + 3)}
    out = _z_score(real)
    assert len(out) == len(real)
    assert abs(sum(out.values())) < 1e-9  # z-scores are mean-zero


# ── Stale data must announce itself ─────────────────────────────────

def test_stale_technicals_are_labelled_stale(monkeypatch):
    """CVX was served a 1963-12-26 RSI under the header 'these are the
    authoritative values; do NOT estimate around them'. Whatever the
    freshness, the block must not claim authority it does not have."""
    from app.quant import technical_baseline as tb

    stale = {
        "ticker": "CVX", "as_of": "1963-12-26", "stale": True, "age_days": 22856,
        "rsi_14": 50.0, "sma_20": 10.0, "sma_50": 10.0, "atr_14": 1.0,
    }
    monkeypatch.setattr(tb, "compute_technical_baseline", lambda t: stale)
    block = tb.build_technical_baseline_block("CVX")

    assert "STALE" in block, block
    assert "1963-12-26" in block, "the observation date must be stated"
    # The authority claim must be absent on a stale baseline — that exact
    # sentence over a 22,856-day-old RSI is what sent the quant analyst
    # reasoning off a 1963 price.
    assert "authoritative" not in block.lower(), block


def test_fresh_technicals_may_claim_authority():
    """The complement: on a genuinely fresh baseline the strong header is
    correct and must survive, or the fix would have cost real grounding."""
    from app.quant import technical_baseline as tb

    fresh = {
        "ticker": "CVX", "as_of": "2026-07-24", "stale": False, "age_days": 0,
        "rsi_14": 71.44, "sma_20": 10.0, "sma_50": 10.0, "atr_14": 1.0,
    }
    import unittest.mock as _m
    with _m.patch.object(tb, "compute_technical_baseline", return_value=fresh):
        block = tb.build_technical_baseline_block("CVX")
    assert "VERIFIED" in block
    assert "STALE" not in block
