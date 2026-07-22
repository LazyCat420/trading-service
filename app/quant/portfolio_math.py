"""
Portfolio-level math: covariance shrinkage, HRP weights, diversification ratio.

Hand-rolled on numpy/scipy because the container ships neither scikit-learn
nor riskfolio-lib, and the equation-library sandbox (numpy/pandas only,
single-ticker df) can't host portfolio math anyway. Pure functions, no I/O —
data loading lives in app/quant/returns.py, tool plumbing in
app/tools/portfolio_tools.py.
"""

from __future__ import annotations

import numpy as np
import scipy.cluster.hierarchy as sch
from scipy.spatial.distance import squareform


def ledoit_wolf_shrinkage(returns: np.ndarray) -> tuple[np.ndarray, float]:
    """Ledoit-Wolf (2004) shrinkage toward the scaled identity.

    Args:
        returns: T x N matrix of (log) returns, one column per asset.

    Returns:
        (shrunk covariance N x N, shrinkage intensity in [0, 1])
    """
    X = np.asarray(returns, dtype=float)
    if X.ndim != 2:
        raise ValueError("returns must be a T x N matrix")
    T, N = X.shape
    if T < 2:
        raise ValueError(f"need at least 2 observations, got {T}")

    X = X - X.mean(axis=0)
    S = X.T @ X / T
    if N == 1:
        return S, 0.0

    # Ledoit-Wolf norms are Frobenius scaled by 1/N.
    mu = float(np.trace(S)) / N
    delta2 = float(np.sum((S - mu * np.eye(N)) ** 2)) / N
    if delta2 <= 1e-18:
        return S, 0.0

    outer = np.einsum("ti,tj->tij", X, X)  # T x N x N of x_t x_t'
    beta2_bar = float(np.sum((outer - S) ** 2)) / (N * T * T)
    beta2 = min(beta2_bar, delta2)
    shrinkage = beta2 / delta2

    cov = shrinkage * mu * np.eye(N) + (1.0 - shrinkage) * S
    return cov, float(shrinkage)


def condition_number(cov: np.ndarray) -> float:
    try:
        return float(np.linalg.cond(np.asarray(cov, dtype=float)))
    except Exception:
        return float("inf")


def cov_to_corr(cov: np.ndarray) -> np.ndarray:
    cov = np.asarray(cov, dtype=float)
    std = np.sqrt(np.clip(np.diag(cov), 1e-12, None))
    corr = cov / np.outer(std, std)
    corr = np.clip(corr, -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    return corr


def _cluster_variance(cov: np.ndarray, items: np.ndarray) -> float:
    """Variance of a cluster under inverse-variance weighting (LdP)."""
    sub = cov[np.ix_(items, items)]
    ivp = 1.0 / np.clip(np.diag(sub), 1e-12, None)
    ivp /= ivp.sum()
    return float(ivp @ sub @ ivp)


def hrp_weights(cov: np.ndarray) -> np.ndarray:
    """Hierarchical Risk Parity (Lopez de Prado 2016).

    Correlation -> distance -> single-linkage tree -> quasi-diagonal order ->
    recursive bisection with inverse-cluster-variance splits. Never inverts
    the covariance matrix, so it stays stable where Markowitz blows up.
    """
    cov = np.asarray(cov, dtype=float)
    n = cov.shape[0]
    if n == 0:
        return np.array([])
    if n == 1:
        return np.array([1.0])

    corr = cov_to_corr(cov)
    dist = np.sqrt(np.clip(0.5 * (1.0 - corr), 0.0, None))
    link = sch.linkage(squareform(dist, checks=False), method="single")
    order = np.asarray(sch.leaves_list(link), dtype=int)

    weights = np.ones(n)
    clusters = [order]
    while clusters:
        next_clusters = []
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            split = len(cluster) // 2
            left, right = cluster[:split], cluster[split:]
            v_left = _cluster_variance(cov, left)
            v_right = _cluster_variance(cov, right)
            total = v_left + v_right
            alpha = 1.0 - (v_left / total) if total > 0 else 0.5
            weights[left] *= alpha
            weights[right] *= 1.0 - alpha
            next_clusters.extend([left, right])
        clusters = next_clusters

    return weights / weights.sum()


def diversification_ratio(weights: np.ndarray, cov: np.ndarray) -> float:
    """DR = (w . sigma) / sqrt(w' Sigma w). 1.0 = no diversification benefit;
    higher = the portfolio's parts hedge each other more."""
    w = np.asarray(weights, dtype=float)
    cov = np.asarray(cov, dtype=float)
    asset_vols = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    port_var = float(w @ cov @ w)
    if port_var <= 0:
        return 1.0
    return float((w @ asset_vols) / np.sqrt(port_var))


def apply_view_tilt(
    weights: dict[str, float],
    views: list[dict],
    strength: float = 0.5,
) -> dict[str, float]:
    """Simplified Black-Litterman-style tilt on a baseline weight vector.

    Each view: {"ticker": str, "direction": "BULLISH"|"BEARISH",
    "confidence": 0-100}. A view multiplies its ticker's baseline weight by
    1 + strength * sign * (confidence / 100), clipped to [0.25, 2.0], then the
    vector renormalizes — so low-confidence views barely move the allocation
    and no view can dominate it. Full BL (equilibrium prior, omega, posterior
    mean-variance) needs Sigma^-1, the exact instability HRP exists to avoid.
    """
    tilted = dict(weights)
    for view in views or []:
        ticker = str(view.get("ticker", "")).upper()
        if ticker not in tilted:
            continue
        direction = str(view.get("direction", "")).upper()
        sign = 1.0 if direction == "BULLISH" else -1.0 if direction == "BEARISH" else 0.0
        try:
            confidence = float(view.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(100.0, confidence))
        multiplier = 1.0 + strength * sign * (confidence / 100.0)
        multiplier = max(0.25, min(2.0, multiplier))
        tilted[ticker] = tilted[ticker] * multiplier

    total = sum(tilted.values())
    if total <= 0:
        return dict(weights)
    return {t: w / total for t, w in tilted.items()}


def rebalance_drift(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    threshold: float = 0.05,
) -> dict:
    """Absolute weight drift per ticker plus the tickers breaching threshold."""
    tickers = sorted(set(current_weights) | set(target_weights))
    drift = {
        t: round(current_weights.get(t, 0.0) - target_weights.get(t, 0.0), 4)
        for t in tickers
    }
    breaches = {t: d for t, d in drift.items() if abs(d) > threshold}
    return {"drift": drift, "breaches": breaches, "threshold": threshold}
