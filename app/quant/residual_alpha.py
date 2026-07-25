"""
Residual-alpha attribution for the V3 decision pipeline.

## The question this answers

Measured 2026-07-25 over 856 desks, the board earned **+1.13% edge** while
simply staying long every ticker it looked at earned **+2.16%**. The pipeline
looked profitable against zero and lost to buy-and-hold. That is the whole
problem in one line: *positive returns are not evidence of skill*.

Residual alpha separates the two. Regress the pipeline's realized per-decision
returns on the factor exposures of the names it picked:

    r_i = alpha + B_momentum*z_i,mom + B_lowvol*z_i,lv + B_beta*z_i,beta
                + B_reversal*z_i,rev + e_i

`alpha` is what the decisions earned that the factors do NOT explain. If the
board is simply buying high-beta names in a rising tape, beta absorbs the
return and alpha collapses toward zero. Only an alpha significantly different
from zero is evidence the reasoning added anything.

The t-stat on alpha uses the same Newey-West correction as `stat_gates` —
overlapping forward windows make the naive t-stat far too generous.

## What this module deliberately does NOT do

It does not gate live trades. A residual-alpha estimate over a few hundred
decisions in a single rising tape is a *diagnostic*, not a risk control;
wiring it to block orders would be acting on n that cannot support it. It
reports, and a human decides.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

import numpy as np

from app.quant import factors as factor_lib
from app.quant.stat_gates import newey_west_tstat, suggest_lag

logger = logging.getLogger(__name__)

# Below this, an OLS with 5 parameters is fitting noise.
MIN_DECISIONS = 30

# Conventional bar for residual alpha being real (matches the plan's t>=2.5).
ALPHA_T_GATE = 2.5


def _ols_with_nw(
    y: np.ndarray,
    X: np.ndarray,
    horizon: int | None = None,
) -> dict:
    """OLS with Newey-West standard errors.

    X must already include the intercept column. Returns coefficients, robust
    standard errors, t-stats and R^2. Uses the pseudo-inverse so a collinear
    design (two factors that happen to be near-identical in this sample)
    degrades gracefully instead of raising.
    """
    n, k = X.shape
    if n <= k:
        return {"ok": False, "reason": f"n={n} <= k={k}"}

    XtX_inv = np.linalg.pinv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    resid = y - X @ beta

    L = suggest_lag(n, horizon)
    # Newey-West meat matrix: S = sum_j w_j * (X'e e'X) at lag j.
    S = (X * resid[:, None]).T @ (X * resid[:, None])
    for j in range(1, L + 1):
        Xe_t = X[j:] * resid[j:, None]
        Xe_tj = X[:-j] * resid[:-j, None]
        Gamma = Xe_t.T @ Xe_tj
        w = 1.0 - j / (L + 1.0)
        S += w * (Gamma + Gamma.T)

    cov = XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        t_stats = np.where(se > 0, beta / se, np.nan)

    ss_res = float(resid @ resid)
    y_dm = y - y.mean()
    ss_tot = float(y_dm @ y_dm)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return {
        "ok": True,
        "n": int(n),
        "k": int(k),
        "lag": int(L),
        "beta": beta.tolist(),
        "std_error": se.tolist(),
        "t_stats": [None if not np.isfinite(t) else float(t) for t in t_stats],
        "r_squared": round(float(r2), 4),
    }


def attribute_returns(
    decisions: list[dict],
    factor_names: tuple[str, ...] = factor_lib.FACTOR_NAMES,
    horizon: int = 7,
) -> dict:
    """Decompose realized decision returns into factor exposure + residual alpha.

    `decisions` is a list of {"ticker", "move_pct", "action", "as_of" (date)}.
    Returns are signed to the DIRECTION TAKEN — a SELL that preceded a fall is
    a positive return — because we are measuring the quality of the decision,
    not the drift of the asset.

    Factor exposures are computed **as of the decision date**, from the
    cross-section of tickers decided on that date. Using today's exposures for
    a decision made two months ago would be look-ahead.
    """
    usable = [
        d for d in decisions
        if d.get("ticker") and d.get("move_pct") is not None
        and str(d.get("action", "")).upper() in ("BUY", "SELL", "HOLD")
    ]
    if len(usable) < MIN_DECISIONS:
        return {"ok": False, "reason": f"need >={MIN_DECISIONS} decisions, got {len(usable)}",
                "n": len(usable)}

    # Group by decision date so each cross-section is ranked against its peers.
    by_date: dict[object, list[dict]] = defaultdict(list)
    for d in usable:
        as_of = d.get("as_of")
        if hasattr(as_of, "date"):
            as_of = as_of.date()
        by_date[as_of].append(d)

    rows: list[tuple[float, list[float]]] = []
    skipped_thin = 0
    for as_of, group in sorted(by_date.items(), key=lambda kv: str(kv[0])):
        tickers = sorted({d["ticker"].strip().upper() for d in group})
        if len(tickers) < factor_lib.MIN_CROSS_SECTION:
            # A 3-name cross-section cannot produce meaningful z-scores.
            skipped_thin += len(group)
            continue
        try:
            fac = factor_lib.compute_factors(
                tickers, as_of=as_of if isinstance(as_of, date) else None
            )
        except Exception as e:
            logger.warning("[ResidualAlpha] factor computation failed for %s: %s", as_of, e)
            skipped_thin += len(group)
            continue
        if not fac:
            skipped_thin += len(group)
            continue

        for d in group:
            tkr = d["ticker"].strip().upper()
            exposures = [fac.get(name, {}).get(tkr) for name in factor_names]
            if any(e is None for e in exposures):
                skipped_thin += 1
                continue
            move = float(d["move_pct"])
            action = str(d.get("action", "")).upper()
            # Sign the return to the direction the decision took.
            signed = -move if action == "SELL" else move
            rows.append((signed, [float(e) for e in exposures]))

    if len(rows) < MIN_DECISIONS:
        return {"ok": False,
                "reason": f"only {len(rows)} decisions had full factor exposures "
                          f"({skipped_thin} skipped as thin)",
                "n": len(rows), "skipped": skipped_thin}

    y = np.array([r[0] for r in rows], dtype=float)
    X_fac = np.array([r[1] for r in rows], dtype=float)
    X = np.column_stack([np.ones(len(rows)), X_fac])

    fit = _ols_with_nw(y, X, horizon=horizon)
    if not fit.get("ok"):
        return {"ok": False, "reason": fit.get("reason", "regression failed"), "n": len(rows)}

    alpha = fit["beta"][0]
    alpha_t = fit["t_stats"][0]
    loadings = {
        name: {
            "beta": round(fit["beta"][i + 1], 4),
            "t_stat": None if fit["t_stats"][i + 1] is None
                      else round(fit["t_stats"][i + 1], 3),
        }
        for i, name in enumerate(factor_names)
    }

    raw_mean = float(y.mean())
    explained = raw_mean - alpha
    return {
        "ok": True,
        "n": len(rows),
        "skipped_thin": skipped_thin,
        "raw_mean_return_pct": round(raw_mean, 4),
        "residual_alpha_pct": round(float(alpha), 4),
        "explained_by_factors_pct": round(float(explained), 4),
        "alpha_t_stat": None if alpha_t is None else round(float(alpha_t), 3),
        "alpha_gate": ALPHA_T_GATE,
        "alpha_is_significant": bool(alpha_t is not None and abs(alpha_t) >= ALPHA_T_GATE),
        "factor_loadings": loadings,
        "r_squared": fit["r_squared"],
        "nw_lag": fit["lag"],
    }


def summarize(report: dict) -> str:
    """One-paragraph human summary — used by the scorecard and the CLI."""
    if not report.get("ok"):
        return f"Residual alpha: NOT COMPUTED — {report.get('reason', 'unknown')}"

    verdict = ("SIGNIFICANT" if report["alpha_is_significant"]
               else "NOT distinguishable from zero")
    lines = [
        f"Residual alpha: {report['residual_alpha_pct']:+.2f}% per decision "
        f"(t={report['alpha_t_stat']}, gate {report['alpha_gate']}) — {verdict}.",
        f"  raw mean {report['raw_mean_return_pct']:+.2f}% = "
        f"{report['explained_by_factors_pct']:+.2f}% explained by factor exposure "
        f"+ {report['residual_alpha_pct']:+.2f}% residual.",
        f"  n={report['n']}, R²={report['r_squared']}, NW lag={report['nw_lag']}",
    ]
    for name, ld in report["factor_loadings"].items():
        lines.append(f"    {name:<10} loading {ld['beta']:+.3f} (t={ld['t_stat']})")
    return "\n".join(lines)
