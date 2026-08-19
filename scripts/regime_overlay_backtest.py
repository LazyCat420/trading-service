#!/usr/bin/env python3
"""Backtest a regime-conditional exposure overlay. Pre-registered:
experiments/exp-2026-08-hmm-regime-overlay.md

## The shape this repo was missing

`scripts/factor_backtest.py` knows one shape: a CROSS-SECTIONAL quintile sort
(rank every ticker, long the top fifth, short the bottom). A regime overlay is
a different animal — a TIME-SERIES conditioner that scales one exposure up and
down through time. Nothing here could run that, so the statistical machinery
(`full_gate`, deflated Sharpe, the trial ledger) had no way to reach it. This
supplies the missing simulation and hands the result to the gates that already
exist rather than inventing new ones.

## Why the DIFFERENCE series and not the overlay's own returns

Testing `overlay` returns directly measures the MARKET. In a rising market an
always-long strategy has a positive mean and clears any test of "is the mean
above zero", so a broken overlay that is simply long most of the time would
pass and look like skill. What the overlay actually claims is that the
CONDITIONING adds value, so what gets gated is

    incremental_t = overlay_return_t - buy_and_hold_return_t

which is zero on every day the overlay is fully invested, and non-zero only
where it acted. That is the series the pre-registration names, and it is the
only one promotion may be decided on.

## Point-in-time

Exposure for session t+1 is set from the posterior fitted on data through t
(`regime_hmm_posteriors.as_of = t`). The overlay can never act on a day it has
already seen. Verified in tests/unit/test_regime_overlay.py.

Usage:
    python scripts/regime_overlay_backtest.py --grade
    python scripts/regime_overlay_backtest.py --grade --sweep --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Per side, on every exposure CHANGE. The repo's standing assumption
# (factor_backtest.py, quant_edge_verifier.py).
COST_BPS_PER_SIDE = 7.5
PRIMARY_THRESHOLD = 0.50
SWEEP_THRESHOLDS = (0.30, 0.40, 0.60, 0.70)
MIN_USABLE_OBS = 200          # pre-registered floor
STRESSED_LABEL = "STRESSED"


def load_aligned_series(ticker: str = "SPY") -> list[dict]:
    """(as_of, P(stressed), next-session return %) — the point-in-time join.

    The posterior for `as_of` is paired with the return of the session that
    comes STRICTLY AFTER it, which is the only return it could have traded.
    """
    from scripts.migration.pg_connection import get_db
    from app.quant.returns import dominant_source_sql

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
        posts = db.execute(
            """
            SELECT as_of, state_probabilities, regime
            FROM regime_hmm_posteriors WHERE ticker = %s ORDER BY as_of ASC
            """,
            [ticker],
        ).fetchall()

    closes = [(d, float(c)) for d, c in prices if c == c]
    by_index = {d: i for i, (d, _) in enumerate(closes)}

    out = []
    for as_of, probs, regime in posts:
        i = by_index.get(as_of)
        # i + 1 is the next session: the first one tradeable on this posterior.
        if i is None or i + 1 >= len(closes):
            continue
        prev_close, next_close = closes[i][1], closes[i + 1][1]
        if not prev_close:
            continue
        p = probs if isinstance(probs, dict) else json.loads(probs or "{}")
        out.append({
            "as_of": as_of,
            "p_stressed": float(p.get(STRESSED_LABEL, 0.0)),
            "regime": regime,
            "next_return_pct": (next_close - prev_close) / prev_close * 100.0,
        })
    return out


def simulate(rows: list[dict], threshold: float) -> dict:
    """Run the overlay. Returns baseline, overlay and incremental series."""
    baseline = np.array([r["next_return_pct"] for r in rows], dtype=float)

    exposures = np.array(
        [0.0 if r["p_stressed"] >= threshold else 1.0 for r in rows], dtype=float
    )
    overlay = exposures * baseline

    # Cost on every exposure CHANGE, including the first move away from the
    # baseline's always-invested state. Charging only on entry would make the
    # overlay look free to switch.
    prev = np.concatenate([[1.0], exposures[:-1]])
    turnover = np.abs(exposures - prev)
    overlay = overlay - turnover * (COST_BPS_PER_SIDE / 100.0)

    return {
        "threshold": threshold,
        "baseline": baseline,
        "overlay": overlay,
        "incremental": overlay - baseline,
        "days_out_of_market": int(np.sum(exposures == 0.0)),
        "switches": int(np.sum(turnover > 0)),
    }


def _max_drawdown_pct(returns_pct: np.ndarray) -> float:
    equity = np.cumprod(1.0 + returns_pct / 100.0)
    peak = np.maximum.accumulate(equity)
    return float(np.min(equity / peak - 1.0) * 100.0)


def _annualized(returns_pct: np.ndarray) -> tuple[float, float]:
    mean = float(np.mean(returns_pct)) * 252.0
    sd = float(np.std(returns_pct, ddof=1))
    sharpe = (float(np.mean(returns_pct)) / sd * np.sqrt(252.0)) if sd > 0 else 0.0
    return mean, sharpe


def report(rows: list[dict], sweep: bool, json_out: str | None) -> int:
    from app.quant.stat_gates import full_gate
    from app.quant.trial_registry import deflated_sharpe_from_registry

    n = len(rows)
    print("\nHMM REGIME OVERLAY — exp-2026-08-hmm-regime-overlay")
    print("=" * 78)
    print(f"  usable observations : {n}")
    print(f"  window              : {rows[0]['as_of']} .. {rows[-1]['as_of']}")
    print(f"  cost                : {COST_BPS_PER_SIDE} bps/side on every switch")

    if n < MIN_USABLE_OBS:
        print(f"\n  INCONCLUSIVE — pre-registered floor is {MIN_USABLE_OBS} "
              f"observations and there are {n}.")
        print("  The registration says report and stop rather than widen the "
              "window to reach significance. Backfill further, then re-run.")

    results = {}
    for threshold in (PRIMARY_THRESHOLD, *(SWEEP_THRESHOLDS if sweep else ())):
        primary = threshold == PRIMARY_THRESHOLD
        sim = simulate(rows, threshold)
        inc = sim["incremental"]

        b_ann, b_sharpe = _annualized(sim["baseline"])
        o_ann, o_sharpe = _annualized(sim["overlay"])
        b_dd, o_dd = _max_drawdown_pct(sim["baseline"]), _max_drawdown_pct(sim["overlay"])

        print("\n" + ("PRIMARY" if primary else "secondary") +
              f" — exposure 0 when P(STRESSED) >= {threshold:.2f}")
        print("-" * 78)
        print(f"  days out of market  : {sim['days_out_of_market']}/{n} "
              f"({sim['days_out_of_market'] / n * 100:.0f}%), {sim['switches']} switches")
        print(f"  buy & hold          : {b_ann:+.2f}%/yr  Sharpe {b_sharpe:+.2f}  maxDD {b_dd:.2f}%")
        print(f"  overlay             : {o_ann:+.2f}%/yr  Sharpe {o_sharpe:+.2f}  maxDD {o_dd:.2f}%")
        print(f"  INCREMENTAL mean    : {np.mean(inc):+.4f}%/day "
              f"({np.mean(inc) * 252:+.2f}%/yr)")

        if sim["days_out_of_market"] == 0:
            print("  -> the overlay NEVER ACTED at this threshold: the incremental "
                  "series is identically zero, so there is nothing to test. This is "
                  "a statement about the posterior, not about the rule.")
            results[str(threshold)] = {"n": n, "verdict": "NEVER_ACTED",
                                       "days_out": 0}
            continue

        gate = full_gate(inc, horizon=1, label=f"overlay@{threshold}")
        dsr = deflated_sharpe_from_registry(
            inc, label=f"regime:hmm_overlay_p{int(threshold * 100)}",
            source="scripts/regime_overlay_backtest.py",
        )
        nw, bs, io = gate["newey_west"], gate["bootstrap"], gate["is_oos"]
        print(f"  full_gate           : {gate['verdict']}")
        print(f"    NW t={nw.get('t_stat')} pass={nw.get('passes')} | "
              f"boot CI=[{bs.get('ci_low')}, {bs.get('ci_high')}] pass={bs.get('passes')} | "
              f"IS/OOS {io.get('is_sharpe')}->{io.get('oos_sharpe')} pass={io.get('passes')}")
        if dsr.get("verdict") != "INSUFFICIENT_DATA":
            print(f"  deflated Sharpe     : {dsr['dsr']} vs luck-implied "
                  f"{dsr['expected_max_sharpe_from_luck']} over {dsr['n_trials']} "
                  f"recorded trials -> {dsr['verdict']}")

        promote = (gate["verdict"] == "PASS" and dsr.get("verdict") == "PASS"
                   and o_dd >= b_dd and float(np.mean(inc)) > 0)
        if primary:
            print(f"\n  PRE-REGISTERED DECISION: "
                  f"{'PROMOTE to shadow' if promote else 'REJECT'}")
            if not promote:
                why = []
                if gate["verdict"] != "PASS":
                    why.append(f"full_gate {gate['verdict']}")
                if dsr.get("verdict") != "PASS":
                    why.append(f"DSR {dsr.get('verdict')}")
                if float(np.mean(inc)) <= 0:
                    why.append("incremental mean <= 0")
                if o_dd < b_dd:
                    why.append("drawdown worse than buy & hold")
                print(f"  reason: {', '.join(why)}")

        results[str(threshold)] = {
            "n": n, "primary": primary, "days_out": sim["days_out_of_market"],
            "switches": sim["switches"],
            "incremental_mean_pct_per_day": round(float(np.mean(inc)), 5),
            "baseline_ann_pct": round(b_ann, 3), "overlay_ann_pct": round(o_ann, 3),
            "baseline_maxdd_pct": round(b_dd, 3), "overlay_maxdd_pct": round(o_dd, 3),
            "gate": gate, "deflated_sharpe": dsr, "promote": bool(promote),
        }

    if sweep:
        print("\n  NOTE: every threshold above is recorded in research_trials as its "
              "own hypothesis, so the deflation already accounts for the sweep. "
              "Promoting the best-looking secondary threshold is exactly the move "
              "the pre-registration forbids.")

    if json_out:
        with open(json_out, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nwrote {json_out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", default="SPY")
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--sweep", action="store_true",
                    help="also report secondary thresholds (each recorded as a trial)")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    if not args.grade:
        ap.error("pass --grade")

    rows = load_aligned_series(args.ticker.upper())
    if not rows:
        print("No aligned posterior/price rows. Run:\n"
              "  python scripts/grade_hmm_regime.py --backfill 250")
        return 1
    return report(rows, args.sweep, args.json_out)


if __name__ == "__main__":
    raise SystemExit(main())
