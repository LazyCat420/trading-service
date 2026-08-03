"""The volatility forecast race — controls before conclusions.

Pre-registered in experiments/exp-2026-08-hmm-vol-forecast-value.md.

A comparison harness is only worth its verdict if it can be shown to detect a
difference that IS there and to stay quiet when one is not. So the load-bearing
tests here are the two controls:

  * test_dm_detects_a_genuinely_better_forecaster — a forecaster given the true
    conditional variance must beat one that is systematically wrong, and the
    test must say so with the right SIGN.
  * test_dm_is_quiet_when_two_forecasters_are_identical — the same series
    against itself must not produce a winner.

Without both, a "no significant difference" result is unreadable: it could mean
the models tie, or that the harness cannot tell anything apart.
"""

import math

import numpy as np
import pytest

from scripts.vol_forecast_race import (
    TRADING_DAYS,
    _VAR_FLOOR,
    buy_and_hold,
    diebold_mariano,
    mse,
    qlike,
    size_and_score,
)


# ── loss functions ───────────────────────────────────────────────────

def test_qlike_is_minimised_at_the_true_variance():
    """The defining property. If a loss is not minimised in expectation by the
    truth, ranking forecasts with it is meaningless."""
    rng = np.random.default_rng(20260803)
    true_sigma = 1.3
    r = rng.normal(0.0, true_sigma, 200_000)

    losses = {s: float(np.mean(qlike(np.full(r.size, s), r)))
              for s in (0.8, 1.0, 1.3, 1.6, 2.2)}
    best = min(losses, key=losses.get)
    assert best == pytest.approx(true_sigma, abs=1e-9), losses


def test_qlike_punishes_under_forecasting_harder():
    """The asymmetry the registration relies on: saying risk is half what it
    is must hurt more than saying it is double."""
    rng = np.random.default_rng(7)
    r = rng.normal(0.0, 1.0, 100_000)
    under = float(np.mean(qlike(np.full(r.size, 0.5), r)))
    over = float(np.mean(qlike(np.full(r.size, 2.0), r)))
    assert under > over


def test_mse_is_also_minimised_at_the_truth():
    rng = np.random.default_rng(11)
    r = rng.normal(0.0, 1.0, 200_000)
    losses = {s: float(np.mean(mse(np.full(r.size, s), r))) for s in (0.6, 1.0, 1.5)}
    assert min(losses, key=losses.get) == 1.0


def test_variance_floor_stops_a_zero_forecast_exploding():
    out = qlike(np.array([0.0]), np.array([1.0]))
    assert np.isfinite(out).all()
    assert out[0] == pytest.approx(math.log(_VAR_FLOOR) + 1.0 / _VAR_FLOOR)


# ── the controls ─────────────────────────────────────────────────────

def test_dm_detects_a_genuinely_better_forecaster():
    """POSITIVE CONTROL. Forecaster A is handed the true conditional sigma;
    B is handed a constant. A must win, and `a_wins` must be the flag set."""
    rng = np.random.default_rng(2026)
    n = 1000
    true_sigma = np.where(rng.random(n) < 0.2, 3.0, 1.0)     # two vol regimes
    r = rng.normal(0.0, true_sigma)

    good = qlike(true_sigma, r)
    bad = qlike(np.full(n, float(np.mean(true_sigma))), r)

    dm = diebold_mariano(good, bad, "good_vs_bad")
    assert dm["mean_differential"] < 0
    assert dm["t_stat"] <= -2.0, dm
    assert dm["a_wins"] and not dm["b_wins"]


def test_dm_reports_the_right_side_when_the_order_is_flipped():
    """Guards a sign error, which would silently invert every verdict."""
    rng = np.random.default_rng(2026)
    n = 1000
    true_sigma = np.where(rng.random(n) < 0.2, 3.0, 1.0)
    r = rng.normal(0.0, true_sigma)
    good, bad = qlike(true_sigma, r), qlike(np.full(n, float(np.mean(true_sigma))), r)

    flipped = diebold_mariano(bad, good, "bad_vs_good")
    assert flipped["mean_differential"] > 0
    assert flipped["b_wins"] and not flipped["a_wins"]


def test_dm_is_quiet_when_two_forecasters_are_identical():
    """NEGATIVE CONTROL. Without this, "no significant difference" could just
    mean the harness never finds anything."""
    rng = np.random.default_rng(5)
    r = rng.normal(0.0, 1.0, 800)
    loss = qlike(np.full(r.size, 1.0), r)

    dm = diebold_mariano(loss, loss.copy(), "self")
    assert dm["mean_differential"] == pytest.approx(0.0, abs=1e-12)
    assert not dm["a_wins"] and not dm["b_wins"]


def test_dm_is_quiet_for_a_trivially_small_edge():
    """A forecaster better by a hair must not clear a t of 2 at n=250 — the
    bar has to be hard enough that the live result means something."""
    rng = np.random.default_rng(9)
    r = rng.normal(0.0, 1.0, 250)
    a = qlike(np.full(250, 1.0), r)
    b = qlike(np.full(250, 1.001), r)
    dm = diebold_mariano(a, b, "hair")
    assert not dm["a_wins"] and not dm["b_wins"]


# ── sizing (secondary) ───────────────────────────────────────────────

def _rows(sigmas, rets):
    return [{"as_of": f"d{i}", "hmm_sigma": s, "garch_sigma": s,
             "trail_sigma": s, "realized_pct": r}
            for i, (s, r) in enumerate(zip(sigmas, rets))]


def test_sizing_cuts_exposure_when_forecast_vol_is_high():
    """target/forecast, capped at 1: a forecast at target gives full size, a
    forecast at double target gives half."""
    target_daily = 11.0 / math.sqrt(TRADING_DAYS)
    rows = _rows([target_daily, target_daily * 2.0], [1.0, 1.0])
    out = size_and_score(rows, "hmm_sigma")
    assert out["mean_exposure"] == pytest.approx((1.0 + 0.5) / 2, abs=1e-3)


def test_sizing_never_leverages_above_one():
    """Cap is pre-registered. A tiny forecast must not produce 5x exposure."""
    rows = _rows([0.001] * 30, [0.1] * 30)
    assert size_and_score(rows, "hmm_sigma")["mean_exposure"] <= 1.0


def test_a_constant_forecast_sizing_is_just_scaled_buy_and_hold():
    """Sanity: with a flat forecast the rule adds nothing but a constant
    multiplier, so Sharpe must match buy-and-hold (costs aside)."""
    rng = np.random.default_rng(3)
    rets = rng.normal(0.05, 1.0, 300)
    sigma = 11.0 / math.sqrt(TRADING_DAYS)
    rows = _rows([sigma] * 300, rets)
    sized, bh = size_and_score(rows, "hmm_sigma"), buy_and_hold(rows)
    assert sized["mean_exposure"] == pytest.approx(1.0, abs=1e-6)
    assert sized["sharpe"] == pytest.approx(bh["sharpe"], abs=0.01)


def test_turnover_is_charged():
    target_daily = 11.0 / math.sqrt(TRADING_DAYS)
    flat = _rows([target_daily] * 40, [0.0] * 40)
    churn = _rows([target_daily, target_daily * 4] * 20, [0.0] * 40)
    assert size_and_score(flat, "hmm_sigma")["ann_return_pct"] == pytest.approx(0.0, abs=1e-9)
    assert size_and_score(churn, "hmm_sigma")["ann_return_pct"] < 0


# ── the desk line carries the measured limits ────────────────────────

def test_desk_line_states_the_measured_limits(monkeypatch):
    """The pre-registered consequence of exp-2026-08-hmm-vol-forecast-value.

    The race found the HMM's vol number is not more accurate than a free
    20-day standard deviation (QLIKE: no significant difference; MSE:
    significantly worse) and runs ~1.9 points hot. An unqualified number in a
    prompt is read as authoritative regardless of what any docstring says, so
    the limits travel with it.
    """
    from app.quant import regime_hmm

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

    assert "MEASURED LIMITS" in line
    assert "20-day standard deviation" in line
    assert "58%" in line and "59%" in line          # the direction result
    assert "do not treat the vol number as a forecast edge" in line
    # ...while the outputs that ARE the model's own survive.
    assert "CALM" in line and "34.1" in line
