"""Shared HMM-regime grading math — point-in-time joins and the predictive band.

Extracted from scripts/grade_hmm_regime.py (2026-08-03) so the in-process
component-health monitor can grade the stored posteriors on the same
definitions the offline scripts use. One implementation, three consumers:
scripts/grade_hmm_regime.py, scripts/vol_forecast_race.py, and
app/autoresearch/component_health.py. The script re-exports these names, so
its CLI and the existing tests are unchanged.

Everything here is pure computation over already-fetched rows except the two
loaders, which read price_history / regime_hmm_posteriors.
"""

from __future__ import annotations

import json
import math
from app.db import mongo_query

HORIZON_DAYS = 5
SPX_DEADBAND_PCT = 1.0
TRADING_DAYS_YEAR = 252
# Two-sided normal band for a 95% interval -> 5% expected breach rate.
Z_95 = 1.959964
EXPECTED_BREACH_RATE = 0.05


# ── data access ──────────────────────────────────────────────────────

def market_closes(ticker: str) -> list[tuple]:
    """The SAME single-vendor series the HMM was fitted on."""
    from app.db.connection import get_db
    from app.quant.returns import dominant_source_sql

    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT date, close FROM price_history
            WHERE ticker = %(ticker)s AND close IS NOT NULL AND close > 0
              AND source = ({dominant_source_sql()})
            ORDER BY date ASC
            """,
            # `ticker` is the name dominant_source_sql()'s embedded subquery
            # binds too — renaming it here silently breaks the vendor pin.
            {"ticker": ticker},
        ).fetchall()
    return [(d, float(c)) for d, c in rows if c == c]


def load_posteriors(ticker: str) -> list[dict]:
    from app.db.connection import get_db

    with get_db() as db:
        rows = mongo_query.find_rows('regime_hmm_posteriors', {'ticker': ticker}, ['as_of', 'regime', 'confidence', 'mean_daily_return_pct', 'annualized_vol_pct', 'state_probabilities', 'state_stats', 'transition_matrix', 'stale_sessions'], sort=[('as_of', 1)])

    def _j(v):
        return json.loads(v) if isinstance(v, (str, bytes)) else v

    return [
        {
            "as_of": r[0], "regime": r[1], "confidence": r[2],
            "mean_daily_return_pct": r[3], "annualized_vol_pct": r[4],
            "state_probabilities": _j(r[5]), "state_stats": _j(r[6]),
            "transition_matrix": _j(r[7]), "stale_sessions": r[8],
        }
        for r in rows
    ]


# ── point-in-time forward joins ──────────────────────────────────────

def move_after(closes: list[tuple], start_date, days: int) -> float | None:
    """Percent move over `days` sessions starting from the first close STRICTLY
    after `start_date` — the posterior for date D was fitted on D's close, so
    the tradeable window opens on D+1. Using `>=` here would let the model
    score a move it had already seen."""
    idx = next((i for i, (d, _) in enumerate(closes) if d > start_date), None)
    if idx is None or idx + days >= len(closes):
        return None
    start, end = closes[idx][1], closes[idx + days][1]
    return None if not start else (end - start) / start * 100.0


def next_return_pct(closes: list[tuple], after_date) -> float | None:
    """The single next-session return strictly after `after_date`, in percent."""
    idx = next((i for i, (d, _) in enumerate(closes) if d > after_date), None)
    if idx is None or idx == 0 or idx >= len(closes):
        return None
    prev, cur = closes[idx - 1][1], closes[idx][1]
    return None if not prev else (cur - prev) / prev * 100.0


# ── the model's one-step predictive band ─────────────────────────────

def predictive_band(row: dict) -> float | None:
    """95% one-step band in PERCENT from a stored posterior.

    Mixture over where the chain goes NEXT (gamma_T @ A), not the current
    state alone: a 97%-CALM posterior with a 3% chance of jumping to a 35%-vol
    state has a genuinely fatter one-day distribution than CALM's 11%, and
    grading the current state's vol in isolation would quietly test a model
    nobody is running.
    """
    probs = row.get("state_probabilities") or {}
    stats = row.get("state_stats") or {}
    trans = row.get("transition_matrix") or []
    if not probs or not stats:
        return None

    labels = list(stats.keys())
    if not labels:
        return None
    gamma = [float(probs.get(lbl, 0.0)) for lbl in labels]
    if sum(gamma) <= 0:
        return None

    # Advance one step through the transition matrix when it is well-formed;
    # otherwise fall back to the filtered posterior (still better than a
    # single state, and the caller's data is flagged by the shape check).
    nxt = gamma
    if (isinstance(trans, list) and len(trans) == len(labels)
            and all(isinstance(r, list) and len(r) == len(labels) for r in trans)):
        nxt = [sum(gamma[i] * float(trans[i][j]) for i in range(len(labels)))
               for j in range(len(labels))]
    total = sum(nxt)
    if total <= 0:
        return None
    nxt = [p / total for p in nxt]

    # Daily mean and vol in percent, per state.
    mus = [float(stats[lbl].get("mean_daily_return_pct") or 0.0) for lbl in labels]
    sds = [float(stats[lbl].get("annualized_vol_pct") or 0.0)
           / math.sqrt(TRADING_DAYS_YEAR) for lbl in labels]
    if not any(sds):
        return None

    mean = sum(p * m for p, m in zip(nxt, mus))
    second = sum(p * (s * s + m * m) for p, s, m in zip(nxt, sds, mus))
    var = max(second - mean * mean, 0.0)
    if var <= 0:
        return None
    return Z_95 * math.sqrt(var)


def hmm_direction_call(row: dict) -> str:
    """The HMM's 5-day directional claim, in the LLM's vocabulary."""
    mu = row.get("mean_daily_return_pct")
    if mu is None:
        return "FLAT"
    projected = float(mu) * HORIZON_DAYS
    if projected > SPX_DEADBAND_PCT:
        return "UP"
    if projected < -SPX_DEADBAND_PCT:
        return "DOWN"
    return "FLAT"
