"""
Tests pinning the 2026-07-15 deferred-item decisions (plan Section 8).

  8.1 — an EXPLICIT position_size_pct <= 0 from the board/synthesizer means
        "watch, don't trade" (no fallback sizing, no trade attempt).
  8.2 — a debate/board timeout hard-ABORTs the pipeline instead of silently
        degrading to an unmarked HOLD@0.
  8.3 — policy-blocked decisions register NO price triggers; SELL-side
        triggers require an actual position; the dynamic re-analysis trigger
        remains the "watch for entry" mechanism.
"""
from unittest.mock import patch

import pytest

from app.services.pipeline_service import (
    resolve_buy_size_pct,
    resolve_trigger_registration,
)
from app.v3.orchestrator import _check_abort
from app.v3.guardrails import CircuitBreaker
from app.v3.shared_desk import DeskPhase, PhaseOutcome, SharedDesk

MAX_PCT = 0.10


# ── 8.1: explicit size-0 is a watch-only directive ───────────────────

def test_explicit_zero_size_means_no_trade():
    assert resolve_buy_size_pct(0, confidence=80, max_position_size_pct=MAX_PCT) is None


def test_explicit_negative_size_means_no_trade():
    assert resolve_buy_size_pct(-1.5, confidence=80, max_position_size_pct=MAX_PCT) is None


def test_agent_size_honored_and_converted():
    assert resolve_buy_size_pct(4.0, confidence=80, max_position_size_pct=MAX_PCT) == 0.04


def test_agent_size_capped_at_max():
    assert resolve_buy_size_pct(50, confidence=80, max_position_size_pct=MAX_PCT) == MAX_PCT


def test_missing_size_uses_confidence_fallback():
    # 70% confidence → 7% of cash
    assert resolve_buy_size_pct(None, confidence=70, max_position_size_pct=MAX_PCT) == pytest.approx(0.07)


def test_fallback_has_two_pct_floor():
    assert resolve_buy_size_pct(None, confidence=10, max_position_size_pct=MAX_PCT) == 0.02


def test_bool_is_not_a_size():
    # True is an int in Python — must not be read as position_size_pct=1
    assert resolve_buy_size_pct(True, confidence=70, max_position_size_pct=MAX_PCT) == pytest.approx(0.07)


# ── 8.3: trigger registration policy ─────────────────────────────────

def test_policy_blocked_registers_nothing():
    allowed = resolve_trigger_registration(
        policy_action="HOLD_POLICY_BLOCKED_JURY_VETO",
        action="BUY", trade_executed=False, position_held=False, watch_only=False,
    )
    assert allowed == {"sell_side": False, "dynamic": False}


def test_executed_buy_registers_all():
    allowed = resolve_trigger_registration(
        policy_action="EXECUTE_BUY",
        action="BUY", trade_executed=True, position_held=False, watch_only=False,
    )
    assert allowed == {"sell_side": True, "dynamic": True}


def test_watch_only_keeps_dynamic_entry_trigger_but_no_sell_side():
    allowed = resolve_trigger_registration(
        policy_action="EXECUTE_BUY",
        action="BUY", trade_executed=False, position_held=False, watch_only=True,
    )
    assert allowed == {"sell_side": False, "dynamic": True}


def test_hold_on_held_position_may_update_sell_side():
    allowed = resolve_trigger_registration(
        policy_action="HOLD_NO_SIGNAL",
        action="HOLD", trade_executed=False, position_held=True, watch_only=False,
    )
    assert allowed == {"sell_side": True, "dynamic": True}


def test_executed_sell_registers_no_sell_side():
    # Position was just closed — a stop-loss against it would be stale
    allowed = resolve_trigger_registration(
        policy_action="EXECUTE_SELL",
        action="SELL", trade_executed=True, position_held=True, watch_only=False,
    )
    assert allowed == {"sell_side": False, "dynamic": True}


# ── 8.2: debate/board timeout aborts ─────────────────────────────────

@pytest.fixture
def saved_desks():
    """Capture the desk `_check_abort` persists.

    An abort writes the desk to Mongo (`desk_persistence.save_desk`, which
    re-raises on failure), so these tests used to reach the real client. The
    capture is not just a mute: the persisted phase is asserted below, so an
    abort that forgot to save — or saved before marking the desk ABORTED —
    now fails here.
    """
    saved = []
    with patch("app.v3.orchestrator.save_desk", side_effect=saved.append):
        yield saved


def test_timeout_aborts_from_research_done(saved_desks):
    """Bull/bear/judge time out while the desk is at RESEARCH_DONE."""
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-abort")
    desk.advance_phase(DeskPhase.RESEARCH_DONE)
    breaker = CircuitBreaker(max_retries_per_phase=1)

    result = _check_abort(desk, breaker, "bull_argument", PhaseOutcome.TIMED_OUT)

    assert result is not None
    assert desk.phase == DeskPhase.ABORTED
    assert result["action"] == "HOLD"
    assert result["confidence"] == 0
    assert "timed out" in result["v3_metadata"]["abort_reason"]
    # The aborted desk is persisted, and persisted as ABORTED.
    assert saved_desks and saved_desks[-1].phase == DeskPhase.ABORTED


def test_timeout_aborts_from_debate_done(saved_desks):
    """Board of directors times out after the debate layer completed."""
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-abort2")
    desk.advance_phase(DeskPhase.RESEARCH_DONE)
    desk.advance_phase(DeskPhase.DEBATE_DONE)
    breaker = CircuitBreaker(max_retries_per_phase=1)

    result = _check_abort(desk, breaker, "board_of_directors", PhaseOutcome.TIMED_OUT)

    assert result is not None
    assert desk.phase == DeskPhase.ABORTED
    assert result["v3_metadata"]["abort_reason"] == "board_of_directors timed out"
    assert saved_desks and saved_desks[-1].phase == DeskPhase.ABORTED


def test_success_does_not_abort():
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-ok")
    desk.advance_phase(DeskPhase.RESEARCH_DONE)
    breaker = CircuitBreaker(max_retries_per_phase=1)

    assert _check_abort(desk, breaker, "bull_argument", PhaseOutcome.SUCCESS) is None
    assert desk.phase == DeskPhase.RESEARCH_DONE
