"""
Portfolio-math wave (2026-07-21): Ledoit-Wolf shrinkage, HRP weights,
diversification ratio, view tilts, GARCH forecasting, strategy health,
and health-driven sizing.
"""

import numpy as np
import pytest

from app.quant import portfolio_math as pm
from app.quant.garch import garch_forecast
from app.quant.strategy_health import score_series_health, CUT_AVG, REDUCE_AVG
from app.services.pipeline_service import apply_health_sizing


def _correlated_returns(seed=7, T=300, n=4, rho=0.6):
    rng = np.random.default_rng(seed)
    common = rng.normal(0, 0.01, T)
    return np.column_stack([
        np.sqrt(rho) * common + np.sqrt(1 - rho) * rng.normal(0, 0.01, T)
        for _ in range(n)
    ])


# ── Ledoit-Wolf ──────────────────────────────────────────────────────

def test_lw_shrinkage_intensity_in_unit_interval():
    X = _correlated_returns()
    cov, intensity = pm.ledoit_wolf_shrinkage(X)
    assert 0.0 <= intensity <= 1.0
    assert cov.shape == (4, 4)
    assert np.allclose(cov, cov.T)


def test_lw_improves_conditioning_when_samples_scarce():
    # T barely above N: the sample covariance is ill-conditioned and
    # shrinkage must pull the condition number down.
    rng = np.random.default_rng(3)
    X = rng.normal(0, 0.01, (12, 10))
    Xc = X - X.mean(axis=0)
    sample_cov = Xc.T @ Xc / X.shape[0]
    shrunk, intensity = pm.ledoit_wolf_shrinkage(X)
    assert intensity > 0.0
    assert pm.condition_number(shrunk) < pm.condition_number(sample_cov)


def test_lw_single_asset():
    X = np.random.default_rng(1).normal(0, 0.02, (100, 1))
    cov, intensity = pm.ledoit_wolf_shrinkage(X)
    assert cov.shape == (1, 1) and intensity == 0.0


# ── HRP ──────────────────────────────────────────────────────────────

def test_hrp_weights_sum_to_one_and_positive():
    cov, _ = pm.ledoit_wolf_shrinkage(_correlated_returns())
    w = pm.hrp_weights(cov)
    assert w.shape == (4,)
    assert abs(w.sum() - 1.0) < 1e-9
    assert (w > 0).all()


def test_hrp_underweights_the_high_vol_asset():
    # Diagonal covariance, one asset 25x the variance of the rest: inverse
    # variance logic must give it the smallest weight.
    cov = np.diag([0.01, 0.01, 0.01, 0.25])
    w = pm.hrp_weights(cov)
    assert w[3] == w.min()
    assert w[3] < 0.15


def test_hrp_single_asset_is_full_weight():
    assert pm.hrp_weights(np.array([[0.04]])).tolist() == [1.0]


# ── Diversification ratio ────────────────────────────────────────────

def test_dr_is_one_for_single_asset_and_higher_when_uncorrelated():
    cov_id = np.diag([0.04, 0.04, 0.04, 0.04])
    w = np.full(4, 0.25)
    dr_uncorr = pm.diversification_ratio(w, cov_id)
    assert dr_uncorr == pytest.approx(2.0)  # sqrt(n) for iid equal weights
    ones = np.ones((4, 4)) * 0.04  # perfectly correlated
    assert pm.diversification_ratio(w, ones) == pytest.approx(1.0)


# ── View tilt ────────────────────────────────────────────────────────

def test_view_tilt_scales_with_confidence_and_renormalizes():
    base = {"AAPL": 0.5, "MSFT": 0.5}
    strong = pm.apply_view_tilt(base, [{"ticker": "AAPL", "direction": "BULLISH", "confidence": 90}])
    weak = pm.apply_view_tilt(base, [{"ticker": "AAPL", "direction": "BULLISH", "confidence": 10}])
    assert abs(sum(strong.values()) - 1.0) < 1e-9
    assert strong["AAPL"] > weak["AAPL"] > base["AAPL"]
    bearish = pm.apply_view_tilt(base, [{"ticker": "AAPL", "direction": "BEARISH", "confidence": 90}])
    assert bearish["AAPL"] < base["AAPL"]


def test_view_tilt_ignores_unknown_tickers_and_garbage():
    base = {"AAPL": 1.0}
    out = pm.apply_view_tilt(base, [{"ticker": "ZZZ", "direction": "BULLISH", "confidence": 99},
                                    {"ticker": "AAPL", "confidence": "not-a-number"}])
    assert out == {"AAPL": 1.0}


# ── Drift ────────────────────────────────────────────────────────────

def test_rebalance_drift_flags_only_breaches():
    res = pm.rebalance_drift({"A": 0.50, "B": 0.50}, {"A": 0.40, "B": 0.53}, threshold=0.05)
    assert "A" in res["breaches"] and "B" not in res["breaches"]
    assert res["drift"]["A"] == pytest.approx(0.10)


# ── GARCH ────────────────────────────────────────────────────────────

def test_garch_recovers_volatility_clustering():
    rng = np.random.default_rng(42)
    T, omega, alpha, beta = 600, 0.05, 0.10, 0.85
    r = np.empty(T)
    sigma2 = omega / (1 - alpha - beta)
    for t in range(T):
        r[t] = rng.normal(0, np.sqrt(sigma2))
        sigma2 = omega + alpha * r[t] ** 2 + beta * sigma2
    result = garch_forecast(r / 100.0)  # series was generated in percent
    assert "error" not in result
    params = result["garch_params"]
    assert 0.0 < params["persistence"] < 1.0
    assert result["vol_signal"] in ("EXPANSION", "CONTRACTION", "NEUTRAL")
    assert result["predicted_vol_annualized_pct"] > 0


def test_garch_rejects_short_series():
    assert "error" in garch_forecast(np.random.default_rng(0).normal(0, 0.01, 50))


# ── Strategy health ──────────────────────────────────────────────────

def test_health_insufficient_history_is_normal():
    assert score_series_health([80.0] * 5)["status"] == "NORMAL"


def test_health_statuses_by_average():
    assert score_series_health([80.0] * 60)["status"] == "NORMAL"
    assert score_series_health([CUT_AVG - 5] * 60)["status"] == "CUT"
    assert score_series_health([REDUCE_AVG - 5] * 60)["status"] == "REDUCE"


def test_health_detects_downtrend_despite_healthy_average():
    # 85 → 61: average still ~73 (above REDUCE_AVG) but the slope is the signal.
    scores = list(np.linspace(85, 61, 60))
    h = score_series_health(scores)
    assert h["status"] == "REDUCE"
    assert "trending down" in h["reason"]


# ── Health-driven sizing + policy gate ───────────────────────────────

def test_apply_health_sizing():
    assert apply_health_sizing(0.08, "REDUCE") == pytest.approx(0.04)
    assert apply_health_sizing(0.08, "NORMAL") == pytest.approx(0.08)
    assert apply_health_sizing(None, "REDUCE") is None


def _desk():
    from app.v3.shared_desk import SharedDesk
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-test")
    desk.regime_classification = {"summary": "regime ok"}
    desk.final_decision = {
        "action": "BUY",
        "confidence": 80,
        "stop_loss": 100.0,
        "dynamic_trigger": {"type": "trailing_drop", "value": 0.1},
        "position_size_pct": 4.0,
    }
    return desk


def test_gate_blocks_buy_on_cut_health(monkeypatch):
    import app.quant.strategy_health as sh
    from app.v3.orchestrator import _apply_policy_gates
    monkeypatch.setattr(sh, "get_pipeline_health", lambda: {"status": "CUT", "driver": "v3_quant_analyst", "reason": "avg 40"})
    assert _apply_policy_gates(_desk()) == "HOLD_POLICY_BLOCKED_DEGRADED_MODEL"


def test_gate_allows_buy_on_reduce_health(monkeypatch):
    import app.quant.strategy_health as sh
    from app.v3.orchestrator import _apply_policy_gates
    monkeypatch.setattr(sh, "get_pipeline_health", lambda: {"status": "REDUCE", "driver": "x", "reason": "y"})
    assert _apply_policy_gates(_desk()) == "EXECUTE_BUY"


def test_gate_fails_open_when_health_check_raises(monkeypatch):
    import app.quant.strategy_health as sh
    from app.v3.orchestrator import _apply_policy_gates
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(sh, "get_pipeline_health", _boom)
    assert _apply_policy_gates(_desk()) == "EXECUTE_BUY"


def test_gate_never_blocks_sell_on_cut_health(monkeypatch):
    import app.quant.strategy_health as sh
    from app.v3.orchestrator import _apply_policy_gates
    monkeypatch.setattr(sh, "get_pipeline_health", lambda: {"status": "CUT", "driver": "x", "reason": "y"})
    desk = _desk()
    desk.final_decision["action"] = "SELL"
    desk.cycle_metadata["held"] = True
    assert _apply_policy_gates(desk) == "EXECUTE_SELL"
