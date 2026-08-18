"""Grading the HMM regime shadow — point-in-time, mixture band, staleness.

regime_hmm.py's first paragraph says it exists to be "the baseline the LLM
must beat" and that if the LLM wins "the agent is earning its cost; if not, we
learned that cheaply". On 2026-08-03 `grep -rn "hmm" scripts/` returned
nothing — the posterior was fitted every cycle, rendered into a prompt, and
dropped, so the comparison had never been run.

The load-bearing assertion in this file is test_forward_window_opens_strictly
_after_the_fit_date: a grader with look-ahead would score the model on a move
it had already been shown, and would report skill that does not exist.
"""

import math
from datetime import date

import pytest

from app.quant import regime_hmm
from scripts.grade_hmm_regime import (
    Z_95,
    hmm_direction_call,
    _move_after,
    _next_return_pct,
    predictive_band,
)


# ── no look-ahead ────────────────────────────────────────────────────

def _closes():
    return [
        (date(2026, 1, 5), 100.0),
        (date(2026, 1, 6), 101.0),
        (date(2026, 1, 7), 102.0),
        (date(2026, 1, 8), 103.0),
        (date(2026, 1, 9), 104.0),
        (date(2026, 1, 12), 105.0),
    ]


def test_forward_window_opens_strictly_after_the_fit_date():
    """The posterior for date D was fitted ON D's close, so the tradeable
    window starts at D+1. A `>=` here would hand the model a move it had
    already seen and inflate every hit rate in the report."""
    closes = _closes()
    # From 01-06, 2 sessions forward = 01-07 -> 01-09 (102 -> 104).
    got = _move_after(closes, date(2026, 1, 6), 2)
    assert got == pytest.approx((104.0 - 102.0) / 102.0 * 100.0)

    # The naive version starting ON 01-06 would measure 101 -> 103.
    wrong = (103.0 - 101.0) / 101.0 * 100.0
    assert got != pytest.approx(wrong)


def test_next_return_is_the_session_after_the_fit_date():
    r = _next_return_pct(_closes(), date(2026, 1, 6))
    assert r == pytest.approx((102.0 - 101.0) / 101.0 * 100.0)


def test_window_that_has_not_closed_yet_returns_none():
    assert _move_after(_closes(), date(2026, 1, 9), 5) is None


def test_load_market_returns_never_reads_past_as_of():
    """The point-in-time guarantee at its source: the SQL is bounded by
    `date <= as_of`, so a backfilled fit cannot see the future."""
    import inspect

    src = inspect.getsource(regime_hmm.load_market_returns)
    assert "date <= %(end)s" in src
    assert "end = as_of or date.today()" in src


# ── the predictive band is a MIXTURE, not one state ──────────────────

def _posterior(**over):
    row = {
        "state_probabilities": {"CALM": 0.97, "STRESSED": 0.03},
        "state_stats": {
            "CALM": {"mean_daily_return_pct": 0.10, "annualized_vol_pct": 11.0},
            "STRESSED": {"mean_daily_return_pct": -0.23, "annualized_vol_pct": 35.0},
        },
        "transition_matrix": [[0.97, 0.03], [0.21, 0.79]],
        "mean_daily_return_pct": 0.10,
        "annualized_vol_pct": 11.0,
    }
    row.update(over)
    return row


def test_band_is_wider_than_the_current_state_alone():
    """A 97%-CALM posterior still carries a real chance of jumping to a
    35%-vol state. Grading CALM's 11% in isolation would test a model nobody
    is running."""
    band = predictive_band(_posterior())
    calm_only = Z_95 * 11.0 / math.sqrt(252)
    assert band > calm_only


def test_band_widens_as_the_stressed_probability_rises():
    calm = predictive_band(_posterior(
        state_probabilities={"CALM": 0.99, "STRESSED": 0.01}))
    mixed = predictive_band(_posterior(
        state_probabilities={"CALM": 0.60, "STRESSED": 0.40}))
    stressed = predictive_band(_posterior(
        state_probabilities={"CALM": 0.05, "STRESSED": 0.95}))
    assert calm < mixed < stressed


def test_transition_matrix_actually_moves_the_band():
    """Same posterior, different chain dynamics -> different band. If this
    passed regardless, the matrix would be decorative."""
    sticky = predictive_band(_posterior(transition_matrix=[[0.99, 0.01], [0.01, 0.99]]))
    jumpy = predictive_band(_posterior(transition_matrix=[[0.50, 0.50], [0.50, 0.50]]))
    assert jumpy > sticky


def test_malformed_transition_matrix_falls_back_not_crashes():
    band = predictive_band(_posterior(transition_matrix=[[0.9], [0.1, 0.9]]))
    assert band is not None and band > 0


def test_missing_stats_returns_none():
    assert predictive_band({"state_probabilities": {}, "state_stats": {}}) is None
    assert predictive_band({}) is None


# ── the direction call, in the LLM's units ───────────────────────────

def test_direction_uses_the_same_deadband_as_the_llm_grader():
    # 0.1%/day * 5 = 0.5% -> inside the +/-1% band -> FLAT
    assert hmm_direction_call({"mean_daily_return_pct": 0.10}) == "FLAT"
    # 0.3%/day * 5 = 1.5% -> UP
    assert hmm_direction_call({"mean_daily_return_pct": 0.30}) == "UP"
    assert hmm_direction_call({"mean_daily_return_pct": -0.30}) == "DOWN"
    assert hmm_direction_call({}) == "FLAT"


# ── staleness ────────────────────────────────────────────────────────

def test_stale_posterior_is_flagged_and_warned(monkeypatch):
    """Measured 2026-08-03: SPY's dominant vendor stopped at 07-27 while
    asset_prices carried GSPC through 08-03, so the shadow injected into every
    desk was six sessions behind and the block said only "data through
    2026-07-27" — a date with nothing to compare it to."""
    stale = {
        "ok": True, "ticker": "SPY", "as_of": "2026-07-27",
        "stale_sessions": 6, "is_stale": True,
        "n_states": 2, "selected_by": "BIC", "observations": 545,
        "regime": "CALM", "confidence": 97.0,
        "state_probabilities": {"CALM": 0.97, "STRESSED": 0.03},
        "state_stats": {"CALM": {"annualized_vol_pct": 11.1,
                                 "expected_duration_days": 32.4,
                                 "mean_daily_return_pct": 0.11}},
    }
    monkeypatch.setattr(regime_hmm, "_cached_classification", lambda *a, **k: stale)
    line = regime_hmm.build_hmm_context_line()
    assert "6 sessions STALE" in line
    assert "cannot have seen a recent regime change" in line


def test_fresh_posterior_carries_no_stale_warning(monkeypatch):
    fresh = {
        "ok": True, "ticker": "SPY", "as_of": "2026-08-03",
        "stale_sessions": 0, "is_stale": False,
        "n_states": 2, "selected_by": "BIC", "observations": 551,
        "regime": "CALM", "confidence": 86.0,
        "state_probabilities": {"CALM": 0.86, "STRESSED": 0.14},
        "state_stats": {"CALM": {"annualized_vol_pct": 11.3,
                                 "expected_duration_days": 34.1,
                                 "mean_daily_return_pct": 0.11}},
    }
    monkeypatch.setattr(regime_hmm, "_cached_classification", lambda *a, **k: fresh)
    line = regime_hmm.build_hmm_context_line()
    assert "STALE" not in line
    assert "data through 2026-08-03" in line


# ── persistence is fail-open ─────────────────────────────────────────

def test_persist_rejects_a_failed_classification():
    assert regime_hmm.persist_posterior({"ok": False}) is False
    assert regime_hmm.persist_posterior({}) is False
    assert regime_hmm.persist_posterior({"ok": True}) is False   # no as_of


def test_persist_never_raises(monkeypatch):
    """A measurement table must never be able to stop a desk."""
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(regime_hmm, "get_db", _boom)
    ok = regime_hmm.persist_posterior({
        "ok": True, "ticker": "SPY", "as_of": "2026-08-03", "regime": "CALM",
        "state_stats": {"CALM": {}}, "bic_by_states": {}, "n_states": 2,
    })
    assert ok is False
