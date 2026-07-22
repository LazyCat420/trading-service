"""
GARCH(1,1) next-day volatility forecast, fit by MLE via scipy.

Forward-looking replacement for the quant analyst's backward-looking
"ATR vs its 30-day average" volatility regime. Hand-rolled because the
container does not ship the `arch` package and the equation sandbox only
allows numpy/pandas imports — so this lives as a registered tool
(forecast_volatility_garch in quant_tools.py), not a library equation.
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.optimize import minimize

logger = logging.getLogger(__name__)

MIN_OBSERVATIONS = 100
REALIZED_WINDOW = 20
PREMIUM_BAND = 0.10  # |premium| below this is NEUTRAL
TRADING_DAYS = 252


def _nll_and_sigma2(params: np.ndarray, r: np.ndarray, var0: float):
    """Negative log-likelihood of GARCH(1,1) plus the variance path."""
    omega, alpha, beta = params
    if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 0.999:
        return 1e10, None
    sigma2 = np.empty_like(r)
    sigma2[0] = var0
    for t in range(1, r.size):
        sigma2[t] = omega + alpha * r[t - 1] ** 2 + beta * sigma2[t - 1]
    if np.any(sigma2 <= 0) or not np.all(np.isfinite(sigma2)):
        return 1e10, None
    nll = 0.5 * float(np.sum(np.log(sigma2) + r**2 / sigma2))
    return nll, sigma2


def garch_forecast(returns: np.ndarray) -> dict:
    """Fit GARCH(1,1) on daily returns and forecast next-day volatility.

    Args:
        returns: 1-D array of simple or log daily returns (NOT in percent).

    Returns dict with annualized predicted/realized vol, the prediction
    premium, and a vol_signal (EXPANSION / CONTRACTION / NEUTRAL) — or an
    "error" key when the series is too short or the fit fails.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < MIN_OBSERVATIONS:
        return {"error": f"need >= {MIN_OBSERVATIONS} daily returns, got {r.size}"}

    # Work in percent for optimizer conditioning; de-mean once.
    r_pct = (r - r.mean()) * 100.0
    var0 = float(r_pct.var())
    if var0 <= 0:
        return {"error": "zero-variance return series"}

    x0 = np.array([var0 * 0.05, 0.08, 0.90])
    result = minimize(
        lambda p: _nll_and_sigma2(p, r_pct, var0)[0],
        x0,
        method="Nelder-Mead",
        options={"maxiter": 2000, "xatol": 1e-7, "fatol": 1e-7},
    )
    omega, alpha, beta = result.x
    nll, sigma2 = _nll_and_sigma2(result.x, r_pct, var0)
    if sigma2 is None:
        return {"error": "GARCH fit did not converge to a valid parameter set"}

    sigma2_next = omega + alpha * r_pct[-1] ** 2 + beta * sigma2[-1]
    predicted_daily_vol = float(np.sqrt(sigma2_next)) / 100.0
    realized_daily_vol = float(np.std(r[-REALIZED_WINDOW:], ddof=1))
    if realized_daily_vol <= 0:
        return {"error": "zero realized volatility in trailing window"}

    premium = (predicted_daily_vol - realized_daily_vol) / realized_daily_vol
    if premium > PREMIUM_BAND:
        signal = "EXPANSION"
    elif premium < -PREMIUM_BAND:
        signal = "CONTRACTION"
    else:
        signal = "NEUTRAL"

    ann = float(np.sqrt(TRADING_DAYS))
    return {
        "predicted_vol_annualized_pct": round(predicted_daily_vol * ann * 100, 2),
        "realized_vol_annualized_pct": round(realized_daily_vol * ann * 100, 2),
        "prediction_premium": round(float(premium), 4),
        "vol_signal": signal,
        "garch_params": {
            "omega": round(float(omega), 6),
            "alpha": round(float(alpha), 4),
            "beta": round(float(beta), 4),
            "persistence": round(float(alpha + beta), 4),
        },
        "converged": bool(result.success),
        "observations": int(r.size),
    }
