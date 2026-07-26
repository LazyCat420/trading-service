"""PSR / Deflated Sharpe / minimum track record — correcting for the fact that
we ran many trials and reported the best one.

This repo has tested momentum, low-vol, beta, reversal, several sizing rules,
HMM regimes and many agent configurations against the same price history, then
reported the survivors. Selection alone inflates the winner's Sharpe even when
every candidate is worthless — Bailey & Lopez de Prado (2014).

The headline demonstration, reproducible below: draw 100 series of PURE NOISE,
keep the best, and it shows an annualized Sharpe of ~3.3. The PSR says PASS at
0.9995. Only the DSR, which knows 100 trials were run, correctly says FAIL.

`INSUFFICIENT_DATA` is a distinct verdict from `FAIL` throughout, matching the
existing gates in this module: "could not check" must never read as "checked".
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.quant import stat_gates as SG


def _noise(rng, n=250, sd=0.01):
    return rng.normal(0.0, sd, n)


# ── The headline: selection bias on pure noise ──────────────────────

def test_deflated_sharpe_rejects_the_best_of_many_noise_trials():
    """The reason this module exists. If this ever passes, the DSR is not
    deflating and every 'significant' backtest above it is suspect."""
    rng = np.random.default_rng(42)
    best = None
    for _ in range(100):
        r = _noise(rng)
        sr = r.mean() / r.std(ddof=1)
        if best is None or sr > best[0]:
            best = (sr, r)

    naive = SG.probabilistic_sharpe_ratio(best[1])
    deflated = SG.deflated_sharpe_ratio(best[1], n_trials=100)

    assert naive["verdict"] == "PASS", "precondition: the winner looks great naively"
    assert deflated["verdict"] == "FAIL", (
        f"DSR passed pure noise selected from 100 trials (dsr={deflated['dsr']})"
    )


def test_more_trials_deflate_harder():
    """The luck-implied benchmark must rise with the number of trials."""
    rng = np.random.default_rng(7)
    r = rng.normal(0.0008, 0.01, 300)
    few = SG.deflated_sharpe_ratio(r, n_trials=2)
    many = SG.deflated_sharpe_ratio(r, n_trials=500)
    assert many["expected_max_sharpe_from_luck"] > few["expected_max_sharpe_from_luck"]
    assert many["dsr"] <= few["dsr"]


def test_a_single_trial_does_not_deflate():
    """With one trial there is no selection bias, so the DSR benchmark is 0 and
    it should agree with the plain PSR."""
    rng = np.random.default_rng(3)
    r = rng.normal(0.001, 0.01, 300)
    d = SG.deflated_sharpe_ratio(r, n_trials=1)
    p = SG.probabilistic_sharpe_ratio(r)
    assert d["expected_max_sharpe_from_luck"] == 0.0
    assert d["dsr"] == pytest.approx(p["psr"], abs=1e-6)


def test_a_genuinely_strong_series_still_survives_deflation():
    """The complement — a gate that rejects everything is useless. A large,
    real edge must clear even a heavy trial correction."""
    rng = np.random.default_rng(11)
    strong = rng.normal(0.004, 0.01, 500)  # Sharpe/obs ~0.4, very large
    d = SG.deflated_sharpe_ratio(strong, n_trials=50)
    assert d["verdict"] == "PASS", f"a real edge was deflated away: {d}"


# ── PSR mechanics ───────────────────────────────────────────────────

def test_psr_penalizes_negative_skew():
    """Negative skew with fat tails is the "picking up pennies" profile. For a
    positive Sharpe it must LOWER the PSR relative to a symmetric series with
    the same mean and variance."""
    rng = np.random.default_rng(5)
    base = rng.normal(0.001, 0.01, 400)
    skewed = base.copy()
    # One large loss: same rough mean, strongly negative skew.
    skewed[10] = -0.10
    sym = SG.probabilistic_sharpe_ratio(base)
    neg = SG.probabilistic_sharpe_ratio(skewed)
    assert neg["skew"] < sym["skew"]
    assert neg["psr"] < sym["psr"]


def test_psr_rises_with_sample_size():
    """The same edge observed for longer is more believable."""
    rng = np.random.default_rng(13)
    short = rng.normal(0.0015, 0.01, 40)
    long = np.concatenate([short, rng.normal(0.0015, 0.01, 400)])
    assert SG.probabilistic_sharpe_ratio(long)["psr"] > \
           SG.probabilistic_sharpe_ratio(short)["psr"]


def test_psr_of_a_negative_edge_is_low():
    rng = np.random.default_rng(17)
    losing = rng.normal(-0.002, 0.01, 300)
    out = SG.probabilistic_sharpe_ratio(losing)
    assert out["psr"] < 0.5
    assert out["verdict"] == "FAIL"


def test_annualized_sharpe_is_reported_alongside_per_observation():
    """Both are needed: the formula uses per-observation, humans read
    annualized, and silently mixing them overstates significance by
    sqrt(252)."""
    rng = np.random.default_rng(19)
    out = SG.probabilistic_sharpe_ratio(rng.normal(0.001, 0.01, 300))
    assert out["sharpe_annualized"] == pytest.approx(
        out["sharpe_per_obs"] * math.sqrt(SG.TRADING_DAYS_YEAR), rel=1e-3
    )


# ── Minimum track record ────────────────────────────────────────────

def test_min_track_record_flags_a_short_sample():
    rng = np.random.default_rng(23)
    short = rng.normal(0.0005, 0.01, 30)
    out = SG.min_track_record_length(short)
    if out["verdict"] not in ("NEVER", "INSUFFICIENT_DATA"):
        assert out["min_track_record"] > out["n"]
        assert out["shortfall"] > 0


def test_min_track_record_is_never_for_a_negative_edge():
    """No sample size makes a losing strategy significant, and saying "needs
    9,000 more days" would imply otherwise."""
    rng = np.random.default_rng(29)
    out = SG.min_track_record_length(rng.normal(-0.002, 0.01, 200))
    assert out["verdict"] == "NEVER"


def test_a_strong_long_series_passes_the_track_record_gate():
    rng = np.random.default_rng(31)
    out = SG.min_track_record_length(rng.normal(0.004, 0.01, 600))
    assert out["verdict"] == "PASS"


# ── Insufficient data is not failure ────────────────────────────────

@pytest.mark.parametrize("fn", [
    SG.probabilistic_sharpe_ratio,
    lambda r: SG.deflated_sharpe_ratio(r, n_trials=10),
])
def test_short_series_report_insufficient_data_not_fail(fn):
    assert fn([0.01, -0.01, 0.02])["verdict"] == "INSUFFICIENT_DATA"


def test_zero_variance_is_insufficient_data_not_a_perfect_sharpe():
    """A constant series has an infinite Sharpe by naive arithmetic. It must not
    report PASS."""
    out = SG.probabilistic_sharpe_ratio([0.01] * 100)
    assert out["verdict"] == "INSUFFICIENT_DATA"


def test_nan_and_inf_are_dropped_not_propagated():
    rng = np.random.default_rng(37)
    clean = rng.normal(0.001, 0.01, 200)
    dirty = np.concatenate([clean, [float("nan"), float("inf"), float("-inf")]])
    out = SG.probabilistic_sharpe_ratio(dirty)
    assert out["verdict"] in ("PASS", "FAIL")
    assert out["n"] == 200, "non-finite values must be dropped, not counted"


def test_deflated_sharpe_rejects_a_nonsense_trial_count():
    rng = np.random.default_rng(41)
    out = SG.deflated_sharpe_ratio(rng.normal(0, 0.01, 100), n_trials=0)
    assert out["verdict"] == "INSUFFICIENT_DATA"


def test_estimated_trial_variance_is_flagged():
    """When the caller does not supply the spread of Sharpes across trials, the
    proxy used understates deflation — the result must say so rather than
    present itself as fully specified."""
    rng = np.random.default_rng(43)
    out = SG.deflated_sharpe_ratio(rng.normal(0.001, 0.01, 200), n_trials=20)
    assert out["trial_variance_estimated"] is True


def test_never_verdict_carries_no_min_track_record_key():
    """Callers must not assume the key exists. factor_backtest.py crashed with
    KeyError on the first real run because a negative-edge factor returns NEVER
    — caught by running it, not by reading it."""
    rng = np.random.default_rng(53)
    out = SG.min_track_record_length(rng.normal(-0.003, 0.01, 200))
    assert out["verdict"] == "NEVER"
    assert "min_track_record" not in out, (
        "NEVER must not report a required sample size — no amount of data "
        "rescues a negative edge"
    )
