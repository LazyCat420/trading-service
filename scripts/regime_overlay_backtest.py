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

## Where it reads (MongoDB, since the 2026-08-19 cutover)

`price_history` (vendor-pinned — see `load_aligned_series`) and
`regime_hmm_posteriors`. Both were read out of Postgres until this file was
ported; Postgres is now a frozen archive whose last row is the cutover, so a
backtest reading it would be scoring a series that stopped growing. The two
stores were compared per vendor on 2026-08-30 and Mongo is a strict superset
of the archive on both reads: yfinance 8,443 archive rows -> 8,445 in Mongo
before the cutover with none missing, polygon 264 -> 265, and the posteriors
255 -> 255 identical plus 4 written after it.

Usage:
    python scripts/regime_overlay_backtest.py --grade
    python scripts/regime_overlay_backtest.py --grade --sweep --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date as _date, datetime
from typing import Any

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Per side, on every exposure CHANGE. The repo's standing assumption
# (factor_backtest.py, quant_edge_verifier.py).
COST_BPS_PER_SIDE = 7.5
PRIMARY_THRESHOLD = 0.50
SWEEP_THRESHOLDS = (0.30, 0.40, 0.60, 0.70)
MIN_USABLE_OBS = 200          # pre-registered floor
STRESSED_LABEL = "STRESSED"


def _as_day(value: Any) -> datetime | None:
    """One calendar day as a naive midnight datetime — the join key.

    WHY A PORT NEEDS THIS AND THE SQL DID NOT
    -----------------------------------------
    `price_history.date` and `regime_hmm_posteriors.as_of` were both Postgres
    `date` columns, so `by_index.get(as_of)` below compared two values of one
    type and the ORDER BY was a total order on calendar day. Mongo has no
    column types, and the two collections no longer agree. Measured
    2026-08-30: every one of `price_history`'s SPY dates is a BSON date, but 4
    of the 259 `regime_hmm_posteriors` documents hold `as_of` as the STRING
    `"2026-08-19 00:00:00"` — and those 4 are exactly the ones written after
    the cutover. `app/quant/regime_hmm.py:300` writes `str(dates[-1])`, and
    `app/db/date_fields.as_date` only recognises a bare `YYYY-MM-DD`
    (`_ISO_DATE`), so the coercion seam every write passes through hands the
    string straight to the collection.

    Two things break without this, and neither of them raises:

      * `sort=[("as_of", 1)]` orders by BSON TYPE first, where String ranks
        BELOW Date, so the four newest posteriors come back FIRST. The report
        prints `rows[0] .. rows[-1]` as its window and reads
        "2026-08-19 .. 2026-08-17" — a backwards window on a sorted query.
      * a string key never equals a datetime key, so the join misses on those
        four rows and the backtest silently scores 255 observations where the
        collection holds 259. That is not an empty result anyone would notice;
        it is a short one that looks right.

    So the value is normalised here and the ordering re-established in Python,
    which restores exactly what the `date` column gave for free. Anything
    unparseable returns None: a row that cannot be placed on the calendar
    cannot be aligned to a session, and dropping it is what the SQL join did.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    # NOTE the order: datetime subclasses date, so this must come second.
    if isinstance(value, _date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return datetime(parsed.year, parsed.month, parsed.day)
    return None


def load_aligned_series(ticker: str = "SPY") -> list[dict]:
    """(as_of, P(stressed), next-session return %) — the point-in-time join.

    The posterior for `as_of` is paired with the return of the session that
    comes STRICTLY AFTER it, which is the only return it could have traded.

    The price read pins ONE vendor. `price_history`'s primary key is
    `(ticker, date, source)`, so a ticker-date carries a print per vendor:
    SPY has 269 dual-vendor dates in Mongo. Unpinned, `by_index` would collapse
    them — `closes[i + 1]` would be the OTHER VENDOR'S print of the SAME day,
    turning a session return into a vendor spread. `_one_vendor` is the same
    freshest-then-deepest rule `dominant_source_sql()` spelled out in the SQL
    this replaces.
    """
    from app.db import mongo_query
    from app.quant.returns import _one_vendor

    prices = mongo_query.find_rows(
        "price_history",
        # `{"$gt": 0}` is `close IS NOT NULL AND close > 0`: a missing or null
        # field does not satisfy a `$gt`, so both halves of the SQL predicate
        # are carried by the one operator.
        _one_vendor(ticker, {"ticker": ticker, "close": {"$gt": 0}}),
        ["date", "close"],
        sort=[("date", 1)],
    )
    posts = mongo_query.find_rows(
        "regime_hmm_posteriors",
        {"ticker": ticker},
        ["as_of", "state_probabilities", "regime"],
        sort=[("as_of", 1)],
    )

    # Both series are re-keyed to a calendar day and re-sorted in Python. The
    # Mongo sorts above are kept because they let the server use the index and
    # leave only a nearly-sorted list to fix up — but they are NOT the order
    # this depends on. See `_as_day`.
    closes = []
    for d, c in prices:
        day, close = _as_day(d), (float(c) if c is not None else None)
        if day is None or close is None or close != close:   # != itself: NaN
            continue
        closes.append((day, close))

    aligned = [(day, p, r) for day, p, r in ((_as_day(a), p, r) for a, p, r in posts)
               if day is not None]

    # `sorted` is stable, so a duplicated day keeps the order Mongo's index
    # gave it — the same latitude `ORDER BY date` left the query planner.
    closes.sort(key=lambda row: row[0])
    aligned.sort(key=lambda row: row[0])
    by_index = {d: i for i, (d, _) in enumerate(closes)}

    out = []
    for as_of, probs, regime in aligned:
        i = by_index.get(as_of)
        # i + 1 is the next session: the first one tradeable on this posterior.
        if i is None or i + 1 >= len(closes):
            continue
        prev_close, next_close = closes[i][1], closes[i + 1][1]
        if not prev_close:
            continue
        # Mongo stores `state_probabilities` as a subdocument, so this is a
        # dict; the json.loads branch survives for a legacy row that still
        # holds the JSON text Postgres's jsonb column was read back as.
        p = probs if isinstance(probs, dict) else json.loads(probs or "{}")
        out.append({
            # `.date()` and not the datetime: the column was a Postgres `date`
            # and the report prints this value, so a bare day keeps the window
            # line reading "2025-08-05 .. 2026-08-26" rather than gaining a
            # " 00:00:00" the column never had.
            "as_of": as_of.date(),
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
