"""Factor library, statistical gates, residual alpha, and the HMM regime shadow.

These four shipped together on 2026-07-25 as the survivable subset of a
seven-factor + RL graph plan. The bugs each test guards are named inline; the
theme is that a null result must stay legible as a null result rather than
being laundered into a pass.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.quant import factors as factor_lib
from app.quant import regime_hmm, residual_alpha, stat_gates


# ─────────────────────────── factors ───────────────────────────

def _panel(spec: dict[str, list[float]], start="2024-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=len(next(iter(spec.values()))))
    return pd.DataFrame(spec, index=idx)


def test_momentum_skips_the_most_recent_month():
    """12-1, never 12-0: the skip month is what separates momentum from
    short-term reversal, which has the OPPOSITE sign."""
    n = 300
    # Rises steadily for the first 279 sessions, then crashes in the last 21.
    prices = [100.0 + i for i in range(n - 21)] + [
        379.0 - 8 * i for i in range(21)
    ]
    panel = _panel({"AAA": prices})
    mom = factor_lib.momentum_12_1(panel)
    # The crash lands entirely inside the skipped month, so momentum stays
    # positive. Without the skip it would be dragged negative.
    assert mom["AAA"] > 0


def test_low_vol_is_sign_flipped():
    """A CALM name must score HIGHER than a volatile one, or every downstream
    ranking is inverted."""
    calm = [100.0 + 0.01 * i for i in range(120)]
    wild = [100.0 * (1.0 + 0.05 * (-1) ** i) for i in range(120)]
    panel = _panel({"CALM": calm, "WILD": wild})
    lv = factor_lib.low_volatility(panel)
    assert lv["CALM"] > lv["WILD"]


def test_beta_recovers_a_known_slope():
    """A series constructed as exactly 2x the market must regress to beta≈2."""
    rng = np.random.default_rng(7)
    mkt_rets = rng.normal(0, 0.01, 260)
    mkt_px = 100.0 * np.exp(np.cumsum(np.insert(mkt_rets, 0, 0.0)))
    stock_px = 100.0 * np.exp(np.cumsum(np.insert(mkt_rets * 2.0, 0, 0.0)))
    panel = _panel({"STK": list(stock_px)})
    market = _panel({"SPY": list(mkt_px)})["SPY"]
    beta = factor_lib.market_beta(panel, market)
    assert beta["STK"] == pytest.approx(2.0, abs=0.05)


def test_z_score_needs_a_real_cross_section():
    """Fewer than MIN_CROSS_SECTION names yields NOTHING, not zeros. A
    zero-filled factor reads as 'perfectly average' and is a fabrication."""
    assert factor_lib._z_score({"A": 1.0, "B": 2.0}) == {}


def test_z_scores_are_standardized_and_clipped():
    raw = {f"T{i}": float(i) for i in range(10)}
    raw["OUTLIER"] = 1e6
    z = factor_lib._z_score(raw)
    assert abs(z["OUTLIER"]) <= factor_lib.Z_CLIP
    assert len(z) == 11


def test_market_proxy_excluded_from_cross_section():
    """SPY is an INPUT to beta, not a member of the ranking — including it
    would rank the index against the stocks it explains."""
    tickers = [f"T{i}" for i in range(8)]
    px = {t: [100.0 + i * 0.5 for i in range(300)] for t in tickers}
    px["SPY"] = [100.0 + i * 0.4 for i in range(300)]
    with patch.object(factor_lib, "load_price_panel", return_value=_panel(px)):
        out = factor_lib.compute_factors(tickers)
    for scores in out.values():
        assert "SPY" not in scores


# ──────────────────────── statistical gates ────────────────────────

def test_newey_west_is_more_conservative_than_naive_on_overlapping_data():
    """THE reason this module exists. Overlapping windows are autocorrelated;
    the naive t-stat overstates significance on exactly this data."""
    rng = np.random.default_rng(11)
    daily = rng.normal(0.0004, 0.01, 600)
    # Overlapping 7-day sums — what a 7-session forward return series IS.
    overlapping = np.array([daily[i:i + 7].sum() for i in range(len(daily) - 7)])

    naive_t = overlapping.mean() / (overlapping.std(ddof=1) / np.sqrt(overlapping.size))
    nw = stat_gates.newey_west_tstat(overlapping, horizon=7)
    assert nw["ok"]
    assert abs(nw["t_stat"]) < abs(naive_t)
    assert nw["lag"] >= 6           # at least the overlap


def test_pure_noise_fails_every_gate():
    """A null result must read as a null result."""
    rng = np.random.default_rng(3)
    noise = rng.normal(0.0, 0.01, 400)
    report = stat_gates.full_gate(noise, horizon=7, label="noise")
    assert report["verdict"] == "FAIL"
    assert not report["newey_west"]["passes"]


def test_strong_constant_edge_passes():
    rng = np.random.default_rng(5)
    strong = rng.normal(0.02, 0.01, 400)   # ~2 sigma per observation
    report = stat_gates.full_gate(strong, horizon=1, label="strong")
    assert report["newey_west"]["passes"]
    assert report["bootstrap"]["passes"]


def test_insufficient_data_is_not_a_pass():
    """'We could not check' must never be indistinguishable from 'we checked
    and it is fine' — the laundering failure mode in one assertion."""
    report = stat_gates.full_gate([0.01] * 5, label="tiny")
    assert report["verdict"] == "INSUFFICIENT_DATA"
    assert not report["newey_west"]["ok"]


def test_is_oos_rejects_a_strategy_that_only_worked_in_sample():
    edge = np.concatenate([
        np.random.default_rng(1).normal(0.02, 0.01, 300),   # in-sample edge
        np.random.default_rng(2).normal(-0.001, 0.01, 200),  # dies out-of-sample
    ])
    out = stat_gates.is_oos_degradation(edge)
    assert out["ok"] and not out["passes"]
    assert out["oos_sharpe"] < out["is_sharpe"]


def test_negative_is_sharpe_cannot_pass_via_ratio():
    """Two negative Sharpes divide to a POSITIVE retention ratio. Guard it."""
    losing = np.random.default_rng(4).normal(-0.01, 0.01, 400)
    out = stat_gates.is_oos_degradation(losing)
    assert out["ok"] and not out["passes"]
    assert out["retention"] is None


def test_bootstrap_is_deterministic():
    rng = np.random.default_rng(9)
    data = rng.normal(0.005, 0.01, 200)
    a = stat_gates.stationary_bootstrap_ci(data, resamples=500)
    b = stat_gates.stationary_bootstrap_ci(data, resamples=500)
    assert a["ci_low"] == b["ci_low"] and a["ci_high"] == b["ci_high"]


# ───────────────────────── residual alpha ─────────────────────────

def _decisions(n_dates=12, n_tickers=8, alpha=0.0, beta_load=0.0, seed=1):
    """Synthetic decisions whose returns are a known function of exposure."""
    rng = np.random.default_rng(seed)
    tickers = [f"T{i}" for i in range(n_tickers)]
    out, fake = [], {}
    for d in range(n_dates):
        as_of = date(2026, 1, 5 + d)
        z = {t: float(rng.normal()) for t in tickers}
        fake[as_of] = {name: dict(z) for name in factor_lib.FACTOR_NAMES}
        for t in tickers:
            move = alpha + beta_load * z[t] + rng.normal(0, 0.5)
            out.append({"ticker": t, "move_pct": move, "action": "BUY", "as_of": as_of})
    return out, fake


def test_returns_fully_explained_by_factors_yield_no_alpha():
    """The core claim: buying high-exposure names in a favourable tape is NOT
    alpha, however positive the raw return."""
    decisions, fake = _decisions(alpha=0.0, beta_load=3.0, seed=21)

    def _fake(tickers, as_of=None, include_market=True):
        return fake[as_of]

    with patch.object(factor_lib, "compute_factors", side_effect=_fake):
        rep = residual_alpha.attribute_returns(decisions)
    assert rep["ok"]
    assert not rep["alpha_is_significant"]
    assert abs(rep["residual_alpha_pct"]) < 0.3


def test_genuine_alpha_survives_the_factor_regression():
    decisions, fake = _decisions(alpha=2.0, beta_load=1.0, seed=22)

    def _fake(tickers, as_of=None, include_market=True):
        return fake[as_of]

    with patch.object(factor_lib, "compute_factors", side_effect=_fake):
        rep = residual_alpha.attribute_returns(decisions)
    assert rep["ok"]
    assert rep["alpha_is_significant"]
    assert rep["residual_alpha_pct"] == pytest.approx(2.0, abs=0.4)


def test_sell_returns_are_signed_to_the_decision():
    """A SELL before a fall is a GOOD decision. Scoring it as the raw (negative)
    move would penalize the pipeline for being right."""
    decisions, fake = _decisions(alpha=0.0, beta_load=0.0, seed=23)
    for d in decisions:
        d["action"] = "SELL"
        d["move_pct"] = -4.0            # price fell; the SELL was correct

    def _fake(tickers, as_of=None, include_market=True):
        return fake[as_of]

    with patch.object(factor_lib, "compute_factors", side_effect=_fake):
        rep = residual_alpha.attribute_returns(decisions)
    assert rep["ok"]
    assert rep["raw_mean_return_pct"] > 0


def test_too_few_decisions_reports_not_computed():
    rep = residual_alpha.attribute_returns([
        {"ticker": "A", "move_pct": 1.0, "action": "BUY", "as_of": date(2026, 1, 1)}
    ])
    assert not rep["ok"]
    assert "NOT COMPUTED" in residual_alpha.summarize(rep)


# ────────────────────────── HMM regime ──────────────────────────

def _two_regime_series(seed=42):
    """400 calm sessions then 300 stressed — a known two-regime structure."""
    rng = np.random.default_rng(seed)
    return np.concatenate([
        rng.normal(0.0006, 0.005, 400),
        rng.normal(-0.0015, 0.025, 300),
    ])


def test_hmm_recovers_a_known_two_regime_structure():
    fit = regime_hmm.fit_hmm(_two_regime_series(), n_states=2)
    assert fit["ok"] and fit["converged"]
    lo, hi = sorted(fit["variances"])
    assert hi > lo * 4          # the two regimes are clearly separated


def test_hmm_states_are_ordered_by_volatility():
    """Labels must mean the same thing every run, or the regime story changes
    identity between cycles."""
    fit = regime_hmm.fit_hmm(_two_regime_series(), n_states=2)
    ordered, _ = regime_hmm._order_by_volatility(fit)
    assert ordered["labels"] == ["CALM", "STRESSED"]
    assert ordered["variances"][0] < ordered["variances"][1]


def test_hmm_is_deterministic():
    """EM is only locally optimal — a random init would tell a different
    regime story every cycle."""
    x = _two_regime_series()
    a, b = regime_hmm.fit_hmm(x, 2), regime_hmm.fit_hmm(x, 2)
    assert a["loglik"] == pytest.approx(b["loglik"], rel=1e-12)
    assert a["variances"] == pytest.approx(b["variances"], rel=1e-12)


def test_hmm_survives_long_series_without_underflow():
    """Naive (non-log) forward-backward underflows to zero within ~200 daily
    observations, yielding nan. This is the regression guard."""
    rng = np.random.default_rng(8)
    long_series = rng.normal(0.0003, 0.012, 2000)
    fit = regime_hmm.fit_hmm(long_series, n_states=2)
    assert fit["ok"]
    assert np.isfinite(fit["loglik"])


def test_hmm_rejects_short_series():
    assert not regime_hmm.fit_hmm(np.zeros(50), n_states=2)["ok"]


def test_classify_regime_fails_open_on_db_error():
    """Contract: the HMM must never be able to stop a cycle."""
    regime_hmm.reset_cache()
    with patch.object(regime_hmm, "load_market_returns", side_effect=RuntimeError("db down")):
        out = regime_hmm.classify_regime()
        assert regime_hmm.build_hmm_context_line() == ""
    regime_hmm.reset_cache()
    assert not out["ok"]


def test_hmm_classification_is_cached_per_run():
    """THE REGRESSION GUARD. A fit is ~32s and the answer is market-wide, so an
    uncached per-ticker call blew build_quant_math_block's timeout and made the
    WHOLE block fail open — silently dropping GARCH, HRP and the sizing bracket
    too. Caught live 2026-07-25; the failure logged an empty message."""
    regime_hmm.reset_cache()
    fake = {"ok": True, "regime": "CALM", "confidence": 90.0,
            "state_probabilities": {"CALM": 0.9}, "n_states": 2,
            "selected_by": "BIC", "observations": 500, "ticker": "SPY",
            "state_stats": {"CALM": {"annualized_vol_pct": 12.0,
                                     "expected_duration_days": 20.0,
                                     "mean_daily_return_pct": 0.04}}}
    with patch.object(regime_hmm, "classify_regime", return_value=fake) as fit:
        for _ in range(5):
            regime_hmm.build_hmm_context_line()
    assert fit.call_count == 1, "HMM refit per call — the 25s timeout regression"
    regime_hmm.reset_cache()


def test_hmm_failures_are_cached_too():
    """A FAILING fit costs the same ~32s as a successful one. Retrying it per
    ticker is the exact stall the cache exists to prevent."""
    regime_hmm.reset_cache()
    with patch.object(regime_hmm, "classify_regime",
                      return_value={"ok": False, "reason": "nope"}) as fit:
        for _ in range(4):
            assert regime_hmm.build_hmm_context_line() == ""
    assert fit.call_count == 1
    regime_hmm.reset_cache()


def test_quant_block_timeout_budgets_the_hmm_first_call():
    """The orchestrator's timeout must exceed the HMM's uncached cost (~32s).
    Caching alone is not enough — the FIRST ticker of a cycle still pays it."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "app" / "v3" / "orchestrator.py"
    text = src.read_text()
    block = text[text.index("build_quant_math_block"):]
    timeout = int(re.search(r"timeout=(\d+)", block).group(1))
    assert timeout >= 45, (
        f"quant math timeout is {timeout}s; the HMM's first fit alone measured "
        f"~32s, so the block will fail open and drop GARCH/HRP/sizing too"
    )


def test_context_line_is_framed_as_a_shadow():
    """It must not read as a competing directive to the Regime Engine."""
    fake = {
        "ok": True, "ticker": "SPY", "n_states": 2, "selected_by": "BIC",
        "observations": 500, "regime": "CALM", "confidence": 91.0,
        "state_probabilities": {"CALM": 0.91, "STRESSED": 0.09},
        "state_stats": {"CALM": {"annualized_vol_pct": 12.4,
                                 "expected_duration_days": 30.0,
                                 "mean_daily_return_pct": 0.05}},
    }
    regime_hmm.reset_cache()
    with patch.object(regime_hmm, "classify_regime", return_value=fake):
        line = regime_hmm.build_hmm_context_line()
    regime_hmm.reset_cache()
    assert "NOT" in line and "does not override" in line
