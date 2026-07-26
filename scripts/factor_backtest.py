#!/usr/bin/env python3
"""Do the price-derived factors survive statistical gates on long history?

The cheapest possible falsification of the 2026-07-25 factor wave. If momentum
/ low-vol / beta / reversal do not clear Newey-West + bootstrap + IS-OOS across
decades and a wide cross-section, they will not help the V3 pipeline either,
and we learn that in an afternoon instead of the weeks an A/B would cost.

    python scripts/factor_backtest.py --start 1995-01-01 --rebalance 5

## Construction

Each rebalance date, rank the eligible cross-section on the factor, go long the
top quintile and short the bottom, hold `rebalance` sessions, equal-weighted.
The return series is then run through `app.quant.stat_gates.full_gate`.

## ⚠ SURVIVORSHIP BIAS — READ BEFORE BELIEVING A POSITIVE RESULT

`price_history` holds **today's** tickers (2,744, of which only 103 stop before
2026-06 and most of those are collector gaps, not delistings). Companies that
went bankrupt or were acquired are ABSENT. A long-side backtest on such a
universe is biased UPWARD, because every name in it survived to the present.

This matters asymmetrically, and that asymmetry is the whole reason this test
is worth running:

  * A factor that FAILS here is genuinely dead — it could not clear the bar
    even with the bias helping it. That is a real, trustworthy kill.
  * A factor that PASSES here is NOT established. The bias is a plausible
    explanation for the pass on its own.

A long/short spread is *partially* protected — the bias inflates both legs and
cancels in the difference — but not fully: the delisted names would have been
concentrated in the short leg, so the true short return is understated.

Treat a PASS as "not yet falsified", never as "proven".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Minimum names in the cross-section for a quintile sort to mean anything.
MIN_CROSS_SECTION = 50

# Round-trip friction, matching backtest_runner.COST_PCT_PER_SIDE.
COST_PCT_PER_SIDE = 0.075


def load_panel(start: str, end: str | None = None) -> pd.DataFrame:
    """Wide close panel [date x ticker]. Chunked by year to keep the planner
    off a single huge grouped scan — the Postgres container has the Docker
    default 64MB /dev/shm and fans out to parallel workers on big joins."""
    from app.db.connection import get_db

    end = end or str(date.today())
    frames = []
    y0, y1 = int(start[:4]), int(end[:4])
    with get_db() as db:
        for y in range(y0, y1 + 1):
            lo = max(f"{y}-01-01", start)
            hi = min(f"{y}-12-31", end)
            rows = db.execute(
                """
                SELECT ticker, date, close FROM price_history
                WHERE date >= %s AND date <= %s
                  AND close IS NOT NULL AND close > 0
                """,
                [lo, hi],
            ).fetchall()
            if rows:
                frames.append(pd.DataFrame(rows, columns=["ticker", "date", "close"]))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    panel = df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    return panel.sort_index()


def _factor_values(name: str, hist: pd.DataFrame, mkt: pd.Series | None) -> dict:
    """Raw factor value per ticker from history STRICTLY BEFORE the rebalance.

    `hist` must exclude the rebalance bar itself — passing it in is look-ahead.
    """
    out = {}
    if name == "momentum":
        # 12-1: skip the most recent month (short-term reversal has the
        # opposite sign and cancels momentum when included).
        if len(hist) < 273:
            return {}
        start_px, end_px = hist.iloc[-273], hist.iloc[-22]
        valid = start_px.notna() & end_px.notna() & (start_px > 0)
        out = ((end_px - start_px) / start_px)[valid].to_dict()
    elif name == "low_vol":
        if len(hist) < 61:
            return {}
        rets = np.log(hist.iloc[-61:]).diff()
        vol = rets.std(ddof=1)
        out = (-vol[vol.notna() & (vol > 0)]).to_dict()   # sign-flipped: low vol = high score
    elif name == "reversal":
        if len(hist) < 22:
            return {}
        start_px, end_px = hist.iloc[-22], hist.iloc[-1]
        valid = start_px.notna() & end_px.notna() & (start_px > 0)
        out = (-((end_px - start_px) / start_px)[valid]).to_dict()  # flipped: losers bounce
    elif name == "beta":
        if mkt is None or len(hist) < 253:
            return {}
        w = hist.iloc[-253:]
        m = mkt.reindex(w.index)
        mr = np.log(m).diff()
        var_m = mr.var(ddof=1)
        if not np.isfinite(var_m) or var_m <= 0:
            return {}
        sr = np.log(w).diff()
        # Vectorized covariance. `sr.apply(lambda col: col.cov(mr))` runs a
        # pairwise align per column and takes ~minutes across 2,700 tickers —
        # it silently dominated the whole backtest. Pandas aligns the DataFrame
        # against the Series once here instead, and handles ragged NaNs via the
        # pairwise-complete counts.
        mr_c = mr - mr.mean()
        sr_c = sr.sub(sr.mean())
        n_pair = sr.notna().mul(mr.notna(), axis=0).sum()
        cov = sr_c.mul(mr_c, axis=0).sum() / (n_pair - 1).clip(lower=1)
        beta = (cov / var_m)
        out = beta[np.isfinite(beta) & (n_pair >= 60)].to_dict()
    return {k: float(v) for k, v in out.items() if np.isfinite(v)}


def run_factor(panel: pd.DataFrame, name: str, rebalance: int,
               mkt_col: str = "SPY") -> tuple[np.ndarray, list]:
    """Long top-quintile / short bottom-quintile spread returns, net of costs."""
    mkt = panel[mkt_col] if mkt_col in panel.columns else None
    dates = panel.index
    rets, stamps = [], []
    # Start late enough that the longest lookback (momentum, 273) is available.
    for i in range(280, len(dates) - rebalance, rebalance):
        hist = panel.iloc[:i]              # strictly before the rebalance bar
        vals = _factor_values(name, hist, mkt)
        if len(vals) < MIN_CROSS_SECTION:
            continue
        srt = sorted(vals, key=vals.get)
        k = max(1, len(srt) // 5)
        longs, shorts = srt[-k:], srt[:k]

        entry = panel.iloc[i]
        exit_ = panel.iloc[i + rebalance]
        def leg(names):
            e, x = entry[names], exit_[names]
            ok = e.notna() & x.notna() & (e > 0)
            if ok.sum() == 0:
                return None
            return float((((x[ok] - e[ok]) / e[ok])).mean())
        lr, sr_ = leg(longs), leg(shorts)
        if lr is None or sr_ is None:
            continue
        # Long/short: 2 sides in, 2 out.
        gross = (lr - sr_) * 100.0
        rets.append(gross - 4 * COST_PCT_PER_SIDE)
        stamps.append(dates[i])
    return np.array(rets), stamps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="1995-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--rebalance", type=int, default=5, help="holding period in sessions")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    from app.quant.stat_gates import full_gate

    print(f"loading price panel from {args.start} ...", flush=True)
    panel = load_panel(args.start, args.end)
    if panel.empty:
        print("empty panel"); return 1
    print(f"panel: {panel.shape[0]} sessions x {panel.shape[1]} tickers "
          f"({panel.index.min()} .. {panel.index.max()})\n", flush=True)

    print("⚠ SURVIVORSHIP BIAS: this universe is today's listed tickers. Delisted and")
    print("  acquired names are absent, which biases long-side returns UPWARD.")
    print("  A FAIL here is a real kill. A PASS is 'not yet falsified', not 'proven'.\n")

    factor_names = ("momentum", "low_vol", "beta", "reversal")
    # Trials run against this same price history. A floor, not the true count —
    # every prior sweep on the same data also belongs here.
    n_trials = len(factor_names)

    results = {}
    for name in factor_names:
        rets, stamps = run_factor(panel, name, args.rebalance)
        if rets.size < 40:
            print(f"{name:<10} INSUFFICIENT ({rets.size} periods)")
            results[name] = {"n": int(rets.size), "verdict": "INSUFFICIENT_DATA"}
            continue
        g = full_gate(rets, horizon=args.rebalance, label=name)
        ann = rets.mean() * (252.0 / args.rebalance)
        sharpe = (rets.mean() / rets.std(ddof=1) * np.sqrt(252.0 / args.rebalance)
                  if rets.std(ddof=1) > 0 else 0.0)
        print(f"{name:<10} {g['verdict']:<18} n={rets.size:4d}  "
              f"mean={rets.mean():+.3f}%/{args.rebalance}d  ann={ann:+.1f}%  Sharpe={sharpe:.2f}")
        nw, bs, io = g["newey_west"], g["bootstrap"], g["is_oos"]
        print(f"           NW t={nw.get('t_stat')} (lag {nw.get('lag')}) pass={nw.get('passes')}"
              f" | boot CI=[{bs.get('ci_low')}, {bs.get('ci_high')}] p={bs.get('p_value')} pass={bs.get('passes')}"
              f" | IS/OOS {io.get('is_sharpe')}->{io.get('oos_sharpe')} ret={io.get('retention')} pass={io.get('passes')}")

        # Multiple-testing correction. The Sharpe printed above is the winner of
        # however many factors were run against this same price history, and
        # selection alone inflates it — best-of-100 pure-noise series reaches an
        # annualized Sharpe of ~3.3 (see tests/unit/test_multiple_testing_gates).
        # n_trials counts the factors in THIS sweep, which is a floor: every
        # earlier sweep on the same data belongs in that count too, so the real
        # deflation is stronger than what is shown here.
        from app.quant.stat_gates import (
            deflated_sharpe_ratio, min_track_record_length,
        )
        dsr = deflated_sharpe_ratio(rets, n_trials=n_trials)
        trl = min_track_record_length(rets)
        if dsr.get("verdict") != "INSUFFICIENT_DATA":
            # `NEVER` carries no min_track_record: a negative edge cannot be
            # rescued by more data, and printing a required sample size would
            # imply otherwise.
            if trl.get("verdict") == "NEVER":
                trl_note = " | track record: NEVER (edge is negative)"
            elif "min_track_record" in trl:
                trl_note = f" | need {trl['min_track_record']} obs, have {trl['n']}"
            else:
                trl_note = ""
            print(f"           DSR={dsr['dsr']} vs luck-implied Sharpe "
                  f"{dsr['expected_max_sharpe_from_luck']} over {n_trials} trials "
                  f"→ {dsr['verdict']}{trl_note}")
        results[name] = {"n": int(rets.size), "verdict": g["verdict"],
                         "mean_pct": round(float(rets.mean()), 4),
                         "annualized_pct": round(float(ann), 2),
                         "sharpe": round(float(sharpe), 3), "gate": g,
                         "deflated_sharpe": dsr, "min_track_record": trl}

    passed = [k for k, v in results.items() if v.get("verdict") == "PASS"]
    print("\n" + "=" * 78)
    if passed:
        print(f"NOT FALSIFIED: {', '.join(passed)} cleared every gate.")
        print("  Given the survivorship bias above, treat this as 'worth an A/B',")
        print("  NOT as evidence the factor is real.")
    else:
        print("ALL FACTORS FAILED. None cleared the gates even with survivorship bias")
        print("  working in their favour — that is a trustworthy kill. Injecting these")
        print("  exposures into the desk is unlikely to add anything.")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
