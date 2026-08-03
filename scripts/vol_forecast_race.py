#!/usr/bin/env python3
"""Is the HMM's volatility forecast worth 22 seconds? Pre-registered:
experiments/exp-2026-08-hmm-vol-forecast-value.md

Calibration is not skill. `exp-2026-08-hmm-regime-overlay` showed the HMM's
one-day band is calibrated (Kupiec p=0.167) — but a CONSTANT band equal to
SPY's average volatility would be calibrated too, and would forecast nothing.
The question that decides whether the model stays is whether it beats the free
alternatives:

    HMM predictive band    ~22 s   per day
    GARCH(1,1)              0.056 s per day   (400x cheaper, already shipped)
    trailing 20-day sigma   ~0

All three are strictly point-in-time: the forecast for session t+1 is built
only from data through t. The HMM's comes from stored posteriors whose `as_of`
IS t; GARCH is refit on the window ending at t; trailing sigma uses the 20
sessions ending at t.

## Scoring

Truth proxy is the realized squared return on t+1. That proxy is unbiased for
the true variance but extremely noisy, so the loss function has to be one that
still ranks forecasts correctly under that noise:

    QLIKE = log(sigma^2) + r^2 / sigma^2      (primary)
    MSE   = (sigma^2 - r^2)^2                 (secondary)

QLIKE also punishes UNDER-forecasting risk harder than over-forecasting, which
is the asymmetry a desk actually cares about.

## The test

Diebold-Mariano: a Newey-West t-test on the paired per-day loss differential

    d_t = L(HMM)_t - L(competitor)_t

so a NEGATIVE mean means the HMM is better. Paired differentials cancel the
common shock that makes raw returns so noisy, which is exactly why this is
answerable at n~249 while P&L at the same n is not (the desk's MDE is 8.84pp
over 329 effective decisions).

Usage:
    python scripts/vol_forecast_race.py --run
    python scripts/vol_forecast_race.py --run --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRADING_DAYS = 252
TRAILING_WINDOW = 20
GARCH_WINDOW = 504
TARGET_ANN_VOL_PCT = 11.0     # the CALM state's own reported vol; not outcome-derived
COST_BPS_PER_SIDE = 7.5
MIN_PAIRED_DAYS = 200
Z_95 = 1.959964
# Floor on a daily variance forecast, in percent^2. Guards QLIKE's r^2/sigma^2
# term from exploding on a degenerate forecast; 0.01 = a 0.1%/day vol, far
# below anything any of these models emits.
_VAR_FLOOR = 0.01


# ── forecasts ────────────────────────────────────────────────────────

def build_series(ticker: str = "SPY") -> list[dict]:
    """One row per day carrying all three forecasts and the realized outcome.

    Every forecast is for the session AFTER `as_of`, built only from data
    through `as_of`.
    """
    from app.db.connection import get_db
    from app.quant.garch import garch_forecast
    from app.quant.returns import dominant_source_sql
    from scripts.grade_hmm_regime import _load_posteriors, predictive_band

    with get_db() as db:
        prices = db.execute(
            f"""
            SELECT date, close FROM price_history
            WHERE ticker = %(ticker)s AND close IS NOT NULL AND close > 0
              AND source = ({dominant_source_sql()})
            ORDER BY date ASC
            """,
            {"ticker": ticker},
        ).fetchall()

    closes = [(d, float(c)) for d, c in prices if c == c]
    dates = [d for d, _ in closes]
    px = np.array([c for _, c in closes], dtype=float)
    # log returns in PERCENT, aligned so ret_pct[i] is the return INTO dates[i]
    ret_pct = np.concatenate([[np.nan], np.diff(np.log(px)) * 100.0])
    index = {d: i for i, d in enumerate(dates)}

    rows = []
    for post in _load_posteriors(ticker):
        i = index.get(post["as_of"])
        if i is None or i + 1 >= len(dates):
            continue

        # HMM: the stored band is a 95% two-sided move in percent -> daily sigma
        band = predictive_band(post)
        if band is None:
            continue
        hmm_sigma = band / Z_95

        # Trailing realized sigma over the 20 sessions ENDING at as_of.
        window = ret_pct[max(1, i - TRAILING_WINDOW + 1): i + 1]
        window = window[np.isfinite(window)]
        if window.size < TRAILING_WINDOW:
            continue
        trail_sigma = float(np.std(window, ddof=1))

        # GARCH refit on the window ending at as_of. Takes RAW log returns;
        # it rescales internally (garch.py:57), so handing it percent returns
        # would inflate the answer 100x.
        hist = ret_pct[max(1, i - GARCH_WINDOW + 1): i + 1]
        hist = hist[np.isfinite(hist)] / 100.0
        if hist.size < 100:
            continue
        g = garch_forecast(hist)
        if not g or not g.get("converged"):
            continue
        garch_sigma = float(g["predicted_vol_annualized_pct"]) / math.sqrt(TRADING_DAYS)

        realized = float(ret_pct[i + 1])
        if not np.isfinite(realized):
            continue

        rows.append({
            "as_of": post["as_of"],
            "hmm_sigma": hmm_sigma,
            "garch_sigma": garch_sigma,
            "trail_sigma": trail_sigma,
            "realized_pct": realized,
        })
    return rows


# ── losses ───────────────────────────────────────────────────────────

def qlike(sigma: np.ndarray, realized: np.ndarray) -> np.ndarray:
    var = np.maximum(sigma ** 2, _VAR_FLOOR)
    return np.log(var) + (realized ** 2) / var


def mse(sigma: np.ndarray, realized: np.ndarray) -> np.ndarray:
    return (np.maximum(sigma ** 2, _VAR_FLOOR) - realized ** 2) ** 2


def diebold_mariano(loss_a: np.ndarray, loss_b: np.ndarray, label: str) -> dict:
    """DM test on the paired loss differential. Negative mean favours A.

    Implemented as Newey-West on d_t rather than a fresh statistic: DM IS a
    HAC t-test on the differential, and reusing the audited helper keeps one
    definition of the correction in this repo.
    """
    from app.quant.stat_gates import newey_west_tstat, stationary_bootstrap_ci

    d = np.asarray(loss_a, dtype=float) - np.asarray(loss_b, dtype=float)
    d = d[np.isfinite(d)]
    nw = newey_west_tstat(d, horizon=1)
    bs = stationary_bootstrap_ci(d)
    mean = float(np.mean(d)) if d.size else float("nan")
    t = nw.get("t_stat")
    excludes_zero = bool(bs.get("ok") and (
        (bs.get("ci_low") or 0) > 0 or (bs.get("ci_high") or 0) < 0))
    return {
        "label": label, "n": int(d.size), "mean_differential": round(mean, 6),
        "t_stat": t, "lag": nw.get("lag"),
        "ci_low": bs.get("ci_low"), "ci_high": bs.get("ci_high"),
        "ci_excludes_zero": excludes_zero,
        # Pre-registered bar: t <= -2.0 AND the CI excludes zero.
        "a_wins": bool(t is not None and t <= -2.0 and excludes_zero),
        "b_wins": bool(t is not None and t >= 2.0 and excludes_zero),
    }


# ── part 2: sizing ───────────────────────────────────────────────────

def size_and_score(rows: list[dict], key: str) -> dict:
    """Vol-target the exposure on one forecast, then score the equity path."""
    fc_ann = np.array([r[key] for r in rows]) * math.sqrt(TRADING_DAYS)
    exposure = np.minimum(1.0, TARGET_ANN_VOL_PCT / np.maximum(fc_ann, 1e-9))
    realized = np.array([r["realized_pct"] for r in rows])

    prev = np.concatenate([[1.0], exposure[:-1]])
    turnover = np.abs(exposure - prev)
    net = exposure * realized - turnover * (COST_BPS_PER_SIDE / 100.0)

    sd = float(np.std(net, ddof=1))
    equity = np.cumprod(1.0 + net / 100.0)
    peak = np.maximum.accumulate(equity)
    return {
        "ann_return_pct": round(float(np.mean(net)) * TRADING_DAYS, 2),
        "sharpe": round(float(np.mean(net)) / sd * math.sqrt(TRADING_DAYS), 2) if sd > 0 else 0.0,
        "ann_vol_pct": round(sd * math.sqrt(TRADING_DAYS), 2),
        "max_drawdown_pct": round(float(np.min(equity / peak - 1.0)) * 100, 2),
        "mean_exposure": round(float(np.mean(exposure)), 3),
        "turnover": round(float(np.sum(turnover)), 1),
    }


def buy_and_hold(rows: list[dict]) -> dict:
    realized = np.array([r["realized_pct"] for r in rows])
    sd = float(np.std(realized, ddof=1))
    equity = np.cumprod(1.0 + realized / 100.0)
    peak = np.maximum.accumulate(equity)
    return {
        "ann_return_pct": round(float(np.mean(realized)) * TRADING_DAYS, 2),
        "sharpe": round(float(np.mean(realized)) / sd * math.sqrt(TRADING_DAYS), 2) if sd > 0 else 0.0,
        "ann_vol_pct": round(sd * math.sqrt(TRADING_DAYS), 2),
        "max_drawdown_pct": round(float(np.min(equity / peak - 1.0)) * 100, 2),
        "mean_exposure": 1.0, "turnover": 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", default="SPY")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()
    if not args.run:
        ap.error("pass --run")

    from app.quant.trial_registry import record_trial

    rows = build_series(args.ticker.upper())
    if not rows:
        print("No aligned rows. Run scripts/grade_hmm_regime.py --backfill 250 first.")
        return 1

    n = len(rows)
    realized = np.array([r["realized_pct"] for r in rows])
    sig = {k: np.array([r[f"{k}_sigma"] for r in rows]) for k in ("hmm", "garch", "trail")}

    print(f"\nVOLATILITY FORECAST RACE — {args.ticker.upper()}")
    print("=" * 78)
    print(f"  paired days : {n}  ({rows[0]['as_of']} .. {rows[-1]['as_of']})")
    print(f"  realized    : {float(np.std(realized, ddof=1)) * math.sqrt(TRADING_DAYS):.2f}% annualized")
    if n < MIN_PAIRED_DAYS:
        print(f"\n  INCONCLUSIVE — pre-registered floor is {MIN_PAIRED_DAYS} paired days.")

    print("\n  mean forecast (annualized %):")
    for k in ("hmm", "garch", "trail"):
        s = sig[k] * math.sqrt(TRADING_DAYS)
        print(f"    {k:<6} mean {np.mean(s):5.2f}  min {np.min(s):5.2f}  max {np.max(s):5.2f}")

    results = {"n": n, "dm": {}, "sizing": {}}

    for loss_name, loss_fn in (("QLIKE", qlike), ("MSE", mse)):
        losses = {k: loss_fn(sig[k], realized) for k in sig}
        print(f"\n  {loss_name} mean loss (lower is better):")
        for k in ("hmm", "garch", "trail"):
            print(f"    {k:<6} {np.mean(losses[k]):.5f}")

        print(f"  Diebold-Mariano on {loss_name} differentials "
              f"(negative t favours the FIRST named):")
        for a, b in (("hmm", "trail"), ("hmm", "garch"), ("garch", "trail")):
            dm = diebold_mariano(losses[a], losses[b], f"{a}_vs_{b}")
            results["dm"][f"{loss_name}:{a}_vs_{b}"] = dm
            verdict = (f"{a.upper()} better" if dm["a_wins"]
                       else f"{b.upper()} better" if dm["b_wins"]
                       else "no significant difference")
            print(f"    {a:<6} vs {b:<6} t={str(dm['t_stat']):>7}  "
                  f"CI=[{dm['ci_low']}, {dm['ci_high']}]  -> {verdict}")

    # ── part 2 (secondary, non-promotable) ──
    print("\n  SECONDARY — vol-targeted sizing "
          f"(target {TARGET_ANN_VOL_PCT}% ann, cap 1.0, {COST_BPS_PER_SIDE}bps/side):")
    print(f"    {'strategy':<12} {'return':>8} {'Sharpe':>7} {'vol':>7} {'maxDD':>8} {'expo':>6}")
    bh = buy_and_hold(rows)
    results["sizing"]["buy_and_hold"] = bh
    print(f"    {'buy & hold':<12} {bh['ann_return_pct']:>7.2f}% {bh['sharpe']:>7.2f} "
          f"{bh['ann_vol_pct']:>6.2f}% {bh['max_drawdown_pct']:>7.2f}% {bh['mean_exposure']:>6.2f}")
    for k in ("hmm", "garch", "trail"):
        s = size_and_score(rows, f"{k}_sigma")
        results["sizing"][k] = s
        print(f"    {k + '-sized':<12} {s['ann_return_pct']:>7.2f}% {s['sharpe']:>7.2f} "
              f"{s['ann_vol_pct']:>6.2f}% {s['max_drawdown_pct']:>7.2f}% {s['mean_exposure']:>6.2f}")
    print("    (secondary and NON-PROMOTABLE: at this n a Sharpe difference is "
          "not resolvable — see the registration.)")

    # ── pre-registered verdict ──
    primary = results["dm"].get("QLIKE:hmm_vs_trail", {})
    vs_garch = results["dm"].get("QLIKE:hmm_vs_garch", {})
    earns = bool(primary.get("a_wins"))
    beaten_by_garch = bool(vs_garch.get("b_wins"))

    print("\n  PRE-REGISTERED DECISION")
    print("  " + "-" * 74)
    if earns and not beaten_by_garch:
        print("  HMM EARNS ITS COST: it beats trailing sigma on QLIKE and GARCH "
              "does not beat it.")
    elif beaten_by_garch:
        print("  HMM IS REDUNDANT: GARCH(1,1) forecasts better at 1/400th the cost.")
    else:
        print("  HMM IS REDUNDANT on this axis: it does not beat a trailing "
              "20-day standard deviation.")

    for k in ("hmm", "garch", "trail"):
        record_trial(f"vol_forecast:{k}", source="scripts/vol_forecast_race.py")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
