"""
Gaussian HMM regime detection — a MEASURABLE shadow of the LLM regime engine.

## Why this exists (it is not a replacement)

`v3_regime_engine` is the best-calibrated agent in the pipeline: measured
2026-07-25 it scored **+7.65 edge, 85.7% hit, Brier 0.146**. But it produced a
scoreable directional claim in only **7 of 130 desks** — 94% of its output
carries no falsifiable statement, so that 85.7% rests on n=7 and cannot be
distinguished from seven lucky draws.

This module emits a regime posterior **every day, unconditionally, from
prices alone**. That makes it the baseline the LLM must beat. If the LLM's
forward calls beat the HMM once both are measured over the same days, the
agent is earning its cost; if not, we learned that cheaply.

It runs as a SHADOW: written to the desk as context, never overriding the
LLM's classification, never gating a trade.

## Implementation notes

The container has **no sklearn, no hmmlearn, no arch** (see the 2026-07-21
quant wave) — everything here is numpy/scipy by hand:

  - Baum-Welch (EM) in LOG SPACE. Naive forward-backward underflows to zero
    within ~200 observations at daily frequency; log-space with logsumexp is
    the only version that survives a 2-year window.
  - Model selection by BIC across 2 and 3 states. BIC penalizes parameters
    harder than AIC, which matters because a 3-state HMM on noisy daily
    returns will nearly always fit better in-sample.
  - Deterministic initialization (return-quantile buckets, fixed seed) so the
    same window yields the same labels run over run. EM is only
    locally-optimal, so a random start would give a different regime story
    each cycle — the single fastest way to make this untrustworthy.

States are ordered by volatility ascending and labelled CALM / TRANSITIONAL /
STRESSED, so the label means the same thing every run.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

import numpy as np
from scipy.special import logsumexp

from app.db.connection import get_db
from app.quant.returns import dominant_source_sql

logger = logging.getLogger(__name__)

MARKET_PROXY = "SPY"
DEFAULT_LOOKBACK_SESSIONS = 504          # ~2 years of daily data
MIN_OBSERVATIONS = 250                   # ~1 year; below this EM is unstable
MAX_ITERATIONS = 200
CONVERGENCE_TOL = 1e-4
CANDIDATE_STATES = (2, 3)
_SEED = 20260725
_MIN_VARIANCE = 1e-8                     # variance floor: a collapsing state
                                         # otherwise drives likelihood to +inf

STATE_LABELS_3 = ("CALM", "TRANSITIONAL", "STRESSED")
STATE_LABELS_2 = ("CALM", "STRESSED")


def load_market_returns(
    ticker: str = MARKET_PROXY,
    lookback_sessions: int = DEFAULT_LOOKBACK_SESSIONS,
    as_of: date | None = None,
) -> tuple[np.ndarray, list]:
    """Daily log returns for the market proxy, plus their dates."""
    end = as_of or date.today()
    start = end - timedelta(days=int(lookback_sessions * 1.6))
    with get_db() as db:
        # One vendor. Without the filter a dual-source ticker yields two rows
        # per shared date, and the log-returns below are then taken across a
        # pair of same-date prints from different adjustment conventions —
        # which both injects near-zero returns (diluting the variance the HMM
        # regimes are defined by) and manufactures jumps where the convention
        # alternates. A volatility-state model is exactly the consumer this
        # corrupts most.
        rows = db.execute(
            f"""
            SELECT date, close FROM price_history
            WHERE ticker = %(ticker)s AND date >= %(start)s AND date <= %(end)s
              AND close IS NOT NULL AND close > 0
              AND source = ({dominant_source_sql()})
            ORDER BY date ASC
            """,
            {"ticker": ticker.strip().upper(), "start": start, "end": end},
        ).fetchall()

    if len(rows) < 2:
        return np.array([]), []
    dates = [r[0] for r in rows]
    closes = np.array([float(r[1]) for r in rows], dtype=float)
    returns = np.diff(np.log(closes))
    return returns, dates[1:]


def _gaussian_logpdf(x: np.ndarray, mean: float, var: float) -> np.ndarray:
    var = max(float(var), _MIN_VARIANCE)
    return -0.5 * (np.log(2.0 * np.pi * var) + (x - mean) ** 2 / var)


def _init_params(x: np.ndarray, n_states: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Deterministic init: split observations by |return| quantile.

    Sorting by absolute return separates quiet days from violent ones, which
    is exactly the structure a volatility-regime HMM is looking for — a far
    better starting point than random means, and it makes the fit reproducible.
    """
    order = np.argsort(np.abs(x))
    chunks = np.array_split(order, n_states)
    means = np.array([x[c].mean() for c in chunks])
    variances = np.array([max(x[c].var(ddof=0), _MIN_VARIANCE) for c in chunks])

    # Regimes persist: a strong diagonal is the right prior and stops EM
    # wandering into a rapidly-switching solution that fits noise.
    trans = np.full((n_states, n_states), 0.1 / max(1, n_states - 1))
    np.fill_diagonal(trans, 0.9)
    trans /= trans.sum(axis=1, keepdims=True)
    start = np.full(n_states, 1.0 / n_states)
    return start, trans, means, variances


def _forward_backward(
    log_b: np.ndarray, log_pi: np.ndarray, log_A: np.ndarray
) -> tuple[np.ndarray, np.ndarray, float]:
    """Log-space forward-backward. Returns (log_gamma, log_xi_sum, loglik)."""
    T, N = log_b.shape

    log_alpha = np.empty((T, N))
    log_alpha[0] = log_pi + log_b[0]
    for t in range(1, T):
        log_alpha[t] = log_b[t] + logsumexp(log_alpha[t - 1][:, None] + log_A, axis=0)

    log_beta = np.zeros((T, N))
    for t in range(T - 2, -1, -1):
        log_beta[t] = logsumexp(log_A + log_b[t + 1] + log_beta[t + 1], axis=1)

    loglik = float(logsumexp(log_alpha[-1]))
    log_gamma = log_alpha + log_beta - loglik

    # Accumulate xi in log space, then exponentiate once at the end.
    log_xi_sum = np.full((N, N), -np.inf)
    for t in range(T - 1):
        term = (log_alpha[t][:, None] + log_A
                + log_b[t + 1][None, :] + log_beta[t + 1][None, :] - loglik)
        log_xi_sum = np.logaddexp(log_xi_sum, term)

    return log_gamma, log_xi_sum, loglik


def fit_hmm(x: np.ndarray, n_states: int) -> dict:
    """Baum-Welch EM for a Gaussian HMM. Returns fitted parameters + BIC."""
    T = x.size
    if T < MIN_OBSERVATIONS:
        return {"ok": False, "reason": f"need >={MIN_OBSERVATIONS} observations, got {T}"}

    start, trans, means, variances = _init_params(x, n_states)
    log_pi = np.log(start + 1e-300)
    log_A = np.log(trans + 1e-300)

    prev_ll = -np.inf
    loglik = -np.inf
    for iteration in range(MAX_ITERATIONS):
        log_b = np.column_stack([
            _gaussian_logpdf(x, means[i], variances[i]) for i in range(n_states)
        ])
        log_gamma, log_xi_sum, loglik = _forward_backward(log_b, log_pi, log_A)

        gamma = np.exp(log_gamma)
        xi_sum = np.exp(log_xi_sum)

        # M-step
        log_pi = np.log(gamma[0] + 1e-300)
        denom = xi_sum.sum(axis=1, keepdims=True)
        denom[denom <= 0] = 1e-300
        log_A = np.log(xi_sum / denom + 1e-300)

        weights = gamma.sum(axis=0)
        weights[weights <= 0] = 1e-300
        means = (gamma * x[:, None]).sum(axis=0) / weights
        variances = np.array([
            max(float((gamma[:, i] * (x - means[i]) ** 2).sum() / weights[i]), _MIN_VARIANCE)
            for i in range(n_states)
        ])

        if abs(loglik - prev_ll) < CONVERGENCE_TOL:
            break
        prev_ll = loglik

    # free params: transitions N*(N-1), means N, variances N, start N-1
    k = n_states * (n_states - 1) + 2 * n_states + (n_states - 1)
    bic = -2.0 * loglik + k * np.log(T)
    aic = -2.0 * loglik + 2 * k

    return {
        "ok": True,
        "n_states": n_states,
        "loglik": float(loglik),
        "bic": float(bic),
        "aic": float(aic),
        "iterations": iteration + 1,
        "converged": bool(abs(loglik - prev_ll) < CONVERGENCE_TOL),
        "means": means.tolist(),
        "variances": variances.tolist(),
        "trans": np.exp(log_A).tolist(),
        "start": np.exp(log_pi).tolist(),
        "gamma": gamma,
    }


def _order_by_volatility(fit: dict) -> tuple[dict, list[int]]:
    """Relabel states by ascending volatility so labels are stable run-to-run."""
    variances = np.array(fit["variances"])
    order = list(np.argsort(variances))
    n = fit["n_states"]
    labels = STATE_LABELS_3 if n == 3 else STATE_LABELS_2

    trans = np.array(fit["trans"])
    reordered = {
        "means": [fit["means"][i] for i in order],
        "variances": [fit["variances"][i] for i in order],
        "trans": trans[np.ix_(order, order)].tolist(),
        "labels": list(labels[:n]),
        "gamma": fit["gamma"][:, order],
    }
    return reordered, order


def classify_regime(
    ticker: str = MARKET_PROXY,
    lookback_sessions: int = DEFAULT_LOOKBACK_SESSIONS,
    as_of: date | None = None,
) -> dict:
    """Fit 2- and 3-state HMMs, select by BIC, return the current regime.

    Fail-OPEN by contract: every caller treats a non-ok result as "no HMM
    context available" and proceeds. This must never be able to stop a cycle.
    """
    try:
        returns, dates = load_market_returns(ticker, lookback_sessions, as_of)
    except Exception as e:
        logger.warning("[RegimeHMM] price load failed: %s", e)
        return {"ok": False, "reason": f"price load failed: {e}"}

    if returns.size < MIN_OBSERVATIONS:
        return {"ok": False,
                "reason": f"need >={MIN_OBSERVATIONS} sessions, got {returns.size}"}

    fits = []
    for n in CANDIDATE_STATES:
        try:
            f = fit_hmm(returns, n)
            if f.get("ok"):
                fits.append(f)
        except Exception as e:
            logger.warning("[RegimeHMM] %d-state fit failed: %s", n, e)

    if not fits:
        return {"ok": False, "reason": "no HMM fit converged"}

    best = min(fits, key=lambda f: f["bic"])
    ordered, _order = _order_by_volatility(best)

    gamma = ordered["gamma"]
    current_probs = gamma[-1]
    current_idx = int(np.argmax(current_probs))
    labels = ordered["labels"]

    ann = np.sqrt(252.0) * 100.0
    trans = np.array(ordered["trans"])
    # Expected duration of a state = 1/(1 - self-transition).
    persistence = [
        round(float(1.0 / max(1e-9, 1.0 - trans[i, i])), 1)
        for i in range(len(labels))
    ]

    return {
        "ok": True,
        "ticker": ticker.strip().upper(),
        "as_of": str(dates[-1]) if dates else None,
        "n_states": best["n_states"],
        "selected_by": "BIC",
        "bic_by_states": {f["n_states"]: round(f["bic"], 1) for f in fits},
        "converged": best["converged"],
        "iterations": best["iterations"],
        "regime": labels[current_idx],
        "regime_index": current_idx,
        "confidence": round(float(current_probs[current_idx]) * 100, 1),
        "state_probabilities": {
            labels[i]: round(float(current_probs[i]), 4) for i in range(len(labels))
        },
        "state_stats": {
            labels[i]: {
                "mean_daily_return_pct": round(float(ordered["means"][i]) * 100, 4),
                # float() before round(): np.float64 survives round() and then
                # breaks json.dumps when the desk artifact is serialized.
                "annualized_vol_pct": round(float(np.sqrt(ordered["variances"][i]) * ann), 2),
                "expected_duration_days": persistence[i],
            }
            for i in range(len(labels))
        },
        "transition_matrix": [[round(v, 4) for v in row] for row in ordered["trans"]],
        "observations": int(returns.size),
    }


# ── Per-cycle cache ───────────────────────────────────────────────────────
# A fit costs ~32s and the answer is market-wide, so every ticker in a wave
# would otherwise pay it again for an identical result. Guarded by a
# threading.Lock because build_quant_math_block is invoked from
# asyncio.to_thread, i.e. concurrently across tickers. Tickers 2..N block on
# the lock and then read the cache rather than starting their own fit.
#
# Keyed by CYCLE, not by calendar date (2026-07-25 audit). It was date-keyed
# while every document describing it — including this module's own docstring —
# claimed per-cycle. The pipeline runs ~8-9 cycles a day, so the first cycle's
# fit was served to all the rest. Two consequences, both real: an afternoon
# cycle read the morning's posterior with nothing marking it stale, and a
# *cached failure* suppressed the HMM block for every remaining cycle that day.
# `app/v3/regime_cache.py` was already cycle-keyed; this now matches it.
_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 4


def _cache_key(cycle_id: str | None, as_of: date | None) -> str:
    """Cycle id when we have one, else the date. A caller with no cycle_id
    (a script, an ad-hoc report) still gets caching, just coarser."""
    return f"cycle:{cycle_id}" if cycle_id else f"date:{as_of or date.today()}"


def _cached_classification(
    as_of: date | None = None, cycle_id: str | None = None,
) -> dict:
    key = _cache_key(cycle_id, as_of)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    with _CACHE_LOCK:
        # Re-check inside the lock: a concurrent caller may have filled it
        # while this thread waited.
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        result = classify_regime(as_of=as_of)
        # Cache failures too — a failing fit costs the same ~32s as a
        # successful one, and retrying it per ticker is the exact stall this
        # cache exists to prevent. The block just renders nothing.
        #
        # A TTL was tried here and reverted: it reintroduced the per-ticker
        # refit for exactly the expensive case the cache exists for. The
        # sticky-failure problem this was meant to solve is already solved by
        # keying on the CYCLE — a failure now expires when the cycle ends,
        # instead of blinding the block for the rest of the calendar day.
        _CACHE[key] = result
        if len(_CACHE) > _CACHE_MAX:
            for stale in list(_CACHE)[:-_CACHE_MAX]:
                _CACHE.pop(stale, None)
        return result


def reset_cache() -> None:
    """Drop the cache. For tests and for a forced recompute."""
    with _CACHE_LOCK:
        _CACHE.clear()


def build_hmm_context_line(
    as_of: date | None = None, cycle_id: str | None = None,
) -> str:
    """One-paragraph context block for the desk. Empty string on any failure.

    Explicitly framed to the reading agent as a *statistical shadow*, not an
    instruction — it must not read as a competing directive to the regime
    engine's own board_directive.

    CACHED, and that is load-bearing. A fit is **~32s** (two Baum-Welch runs,
    2- and 3-state, over ~550 observations). `build_quant_math_block` runs
    once per TICKER under a 25s timeout, so an uncached call blew the budget
    and made the whole block fail open — silently dropping GARCH, HRP *and*
    the sizing bracket, not just the HMM. Caught 2026-07-25 in the first live
    cycle: all three tickers logged "quant math precompute failed (non-fatal)"
    with an empty message (the signature of asyncio.TimeoutError).

    The result is MARKET-WIDE and identical for every ticker in a cycle, so
    computing it per ticker was always waste — the same reasoning that made
    `app/v3/regime_cache.py` cache the LLM regime engine per cycle. Pass the
    `cycle_id` so the cache actually scopes to the cycle; without one it falls
    back to date-keying, which is what it did everywhere before 2026-07-25.
    """
    try:
        r = _cached_classification(as_of, cycle_id=cycle_id)
    except Exception as e:
        logger.debug("[RegimeHMM] context line failed (non-fatal): %s", e)
        return ""
    if not r or not r.get("ok"):
        return ""

    probs = ", ".join(f"{k} {v:.0%}" for k, v in r["state_probabilities"].items())
    stats = r["state_stats"][r["regime"]]
    # State the observation date. It was computed and then dropped, so the
    # agent could not tell a fit made this cycle from one made hours ago —
    # the same blind spot that let a 71-day-old RSI read as current. The
    # technicals block already warns like this; this one now does too.
    as_of_txt = f", data through {r['as_of']}" if r.get("as_of") else ""
    return (
        f"- HMM regime shadow ({r['n_states']}-state Gaussian HMM on {r['ticker']} "
        f"daily returns, n={r['observations']}, selected by {r['selected_by']}"
        f"{as_of_txt}): "
        f"**{r['regime']}** at {r['confidence']:.0f}% posterior [{probs}]. "
        f"This state historically runs {stats['annualized_vol_pct']:.1f}% annualized vol "
        f"with an expected duration of {stats['expected_duration_days']} days. "
        f"This is a price-only statistical estimate shown for comparison — it is NOT "
        f"a directive and does not override the Regime Engine's classification."
    )
