#!/usr/bin/env python3
"""Grade the HMM regime shadow — the comparison it was built for.

`app/quant/regime_hmm.py` opens by saying it exists to be "the baseline the
LLM must beat", and that if the LLM's forward calls beat it "the agent is
earning its cost; if not, we learned that cheaply". That comparison had never
been run. On 2026-08-03 `grep -rn "hmm" scripts/` returned nothing: the
posterior was fitted every cycle, rendered into a prompt, and dropped.
`scripts/grade_regime_calls.py` graded only the LLM side.

## Two claims, graded differently — and that split is the point

The desk's honest MDE is 8.84pp over 329 effective decisions
(`scripts/power_report.py`), so NOTHING that moves P&L by a few points is
provable there for about a year. The escape route that file recommends is a
self-validating control: grade a model on its OWN output, at a frequency
where n is large. So:

  1. VOLATILITY COVERAGE (primary). The HMM's states ARE volatility states —
     this is its native claim and it makes one every single day. From the
     posterior gamma_T and the transition matrix A we form the true one-step
     predictive mixture, gamma_T @ A, and its variance:

         E[r]   = sum_j p_j mu_j
         Var[r] = sum_j p_j (sigma_j^2 + mu_j^2) - E[r]^2

     which is a real test of the MODEL (transition matrix included), not of
     one state label. A 95% band should be breached on 5% of days; Kupiec's
     proportion-of-failures test settles it. n = one per trading day.

  2. DIRECTION (secondary, head-to-head). Mapped into the SAME shape the LLM
     emits — 5-trading-day SPX direction on a +/-1% deadband, scored against
     the same GSPC closes `grade_regime_calls.py` uses — so the two are
     comparable on the same days. Expect the HMM to look weak here: its state
     means are ~0.1%/day, which over 5 days lands inside the deadband, so it
     says FLAT most of the time. That is a real finding about what this model
     can and cannot claim, not a bug, and it is reported rather than hidden.

## Point-in-time

`classify_regime(as_of=D)` loads prices with `date <= D`, so a backfilled fit
sees only what was knowable on D. The forward window always starts strictly
after D. Verified by tests/unit/test_hmm_grading.py.

Usage:
    python scripts/grade_hmm_regime.py --backfill 250     # fit + store (slow)
    python scripts/grade_hmm_regime.py --grade            # score what's stored
    python scripts/grade_hmm_regime.py --grade --compare  # vs the LLM
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The grading math lives in app/quant/regime_grading.py (2026-08-03) so the
# in-process component-health monitor grades on the SAME definitions this CLI
# does. Re-exported under the old names: vol_forecast_race.py and
# tests/unit/test_hmm_grading.py import them from here.
from app.quant.regime_grading import (  # noqa: E402,F401
    EXPECTED_BREACH_RATE,
    HORIZON_DAYS,
    SPX_DEADBAND_PCT,
    TRADING_DAYS_YEAR,
    Z_95,
    hmm_direction_call,
    load_posteriors as _load_posteriors,
    market_closes as _market_closes,
    move_after as _move_after,
    next_return_pct as _next_return_pct,
    predictive_band,
)


# ── data access ──────────────────────────────────────────────────────

def _asset_closes(symbol: str) -> list[tuple]:
    """asset_prices closes — what grade_regime_calls.py scores the LLM on."""
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            "SELECT date, close FROM asset_prices WHERE symbol = %s "
            "AND close IS NOT NULL ORDER BY date ASC",
            [symbol],
        ).fetchall()
    return [(d, float(c)) for d, c in rows if c == c]


# ── backfill ─────────────────────────────────────────────────────────

def backfill(sessions: int, ticker: str, stride: int = 1) -> int:
    """Fit and store a point-in-time posterior for each of the last N sessions.

    A fit is ~22s, so 250 sessions is ~90 minutes. Idempotent: days already
    stored are skipped, so an interrupted run resumes for free.
    """
    from app.quant.regime_hmm import (
        classify_regime, ensure_posterior_table, persist_posterior,
    )
    from app.db.connection import get_db

    closes = _market_closes(ticker)
    if not closes:
        print(f"No {ticker} price history — cannot backfill.")
        return 1
    days = [d for d, _ in closes][-sessions:][::stride]

    ensure_posterior_table()
    with get_db() as db:
        have = {
            r[0] for r in db.execute(
                "SELECT as_of FROM regime_hmm_posteriors WHERE ticker = %s",
                [ticker],
            ).fetchall()
        }

    todo = [d for d in days if d not in have]
    print(f"{ticker}: {len(days)} sessions requested, {len(have)} already stored, "
          f"{len(todo)} to fit (~{len(todo) * 22 / 60:.0f} min)")

    done = failed = 0
    for i, d in enumerate(todo, 1):
        try:
            res = classify_regime(ticker=ticker, as_of=d)
            if res.get("ok") and persist_posterior(res):
                done += 1
            else:
                failed += 1
                print(f"  [{i}/{len(todo)}] {d}: {res.get('reason', 'persist failed')}")
        except Exception as e:                      # noqa: BLE001
            failed += 1
            print(f"  [{i}/{len(todo)}] {d}: {type(e).__name__}: {e}")
        if i % 10 == 0:
            print(f"  [{i}/{len(todo)}] {d} ok={done} failed={failed}", flush=True)

    print(f"backfill complete: {done} stored, {failed} failed")
    return 0


# ── grading ──────────────────────────────────────────────────────────

def grade(ticker: str, compare: bool, json_out: str | None) -> int:
    from app.quant.stat_gates import coverage_gate

    rows = _load_posteriors(ticker)
    if not rows:
        print("No stored posteriors. Run with --backfill N first.")
        return 1

    market = _market_closes(ticker)
    spx = _asset_closes("GSPC")

    # ── 1. volatility coverage (the model's native claim) ──
    realized, bands, used = [], [], []
    for row in rows:
        band = predictive_band(row)
        nxt = _next_return_pct(market, row["as_of"])
        if band is None or nxt is None:
            continue
        realized.append(nxt)
        bands.append(band)
        used.append((row["as_of"], nxt, band, row["regime"]))

    cov = coverage_gate(realized, bands, EXPECTED_BREACH_RATE,
                        label=f"{ticker} 1-day 95% band")

    print(f"\nHMM VOLATILITY COVERAGE — {ticker}")
    print("=" * 78)
    if not cov.get("ok"):
        print(f"  cannot grade: {cov.get('reason')}")
    else:
        print(f"  observations   {cov['observations']}")
        print(f"  breaches       {cov['breaches']} "
              f"({cov['observed_rate'] * 100:.2f}% vs {cov['expected_rate'] * 100:.0f}% expected)")
        print(f"  Kupiec LR      {cov['lr_statistic']:.3f}  p={cov['p_value']:.4f}")
        print(f"  verdict        {'CALIBRATED' if cov['passes'] else cov['direction'].upper()}")
        if not cov["passes"]:
            print("  -> " + (
                "band too NARROW: the model understates one-day risk"
                if cov["direction"] == "too_narrow" else
                "band too WIDE: the model overstates risk and forecasts little"
            ))

    # ── 2. direction, in the LLM's own units ──
    dir_rows = []
    for row in rows:
        move = _move_after(spx, row["as_of"], HORIZON_DAYS)
        if move is None:
            continue
        realized_dir = ("UP" if move > SPX_DEADBAND_PCT
                        else "DOWN" if move < -SPX_DEADBAND_PCT else "FLAT")
        call = hmm_direction_call(row)
        dir_rows.append({"date": str(row["as_of"]), "call": call,
                         "realized": realized_dir, "move_pct": round(move, 2),
                         "regime": row["regime"]})

    print(f"\nHMM 5-DAY DIRECTION (same deadband + GSPC closes as the LLM grader)")
    print("=" * 78)
    if not dir_rows:
        print("  no windows closed yet")
    else:
        hits = sum(1 for g in dir_rows if g["call"] == g["realized"])
        n = len(dir_rows)
        print(f"  hit rate       {hits}/{n} = {hits / n * 100:.0f}%")
        mix = {}
        for g in dir_rows:
            mix[g["call"]] = mix.get(g["call"], 0) + 1
        spread = ", ".join(f"{k} {v}" for k, v in sorted(mix.items()))
        print(f"  calls emitted  {spread}")
        if len(mix) == 1:
            print("  -> DEGENERATE: one value for every day. A predictor that "
                  "never varies has no skill to measure, whatever its hit rate.")
        base = {}
        for g in dir_rows:
            base[g["realized"]] = base.get(g["realized"], 0) + 1
        top, cnt = max(base.items(), key=lambda kv: kv[1])
        print(f"  always-'{top}'   {cnt}/{n} = {cnt / n * 100:.0f}%  "
              f"<- the free benchmark this must beat")

    # ── 3. head-to-head with the LLM on the SAME days ──
    llm = None
    if compare:
        llm = _grade_llm({g["date"] for g in dir_rows}, spx)
        print(f"\nHEAD-TO-HEAD (LLM regime engine, restricted to the same days)")
        print("=" * 78)
        if not llm or not llm["n"]:
            print("  the LLM produced no scoreable forward_call on these days — "
                  "which is itself the finding this module was written to expose:\n"
                  "  a claim made 7 times in 130 desks cannot be compared to one "
                  "made every day.")
        else:
            print(f"  LLM  {llm['hits']}/{llm['n']} = {llm['hits'] / llm['n'] * 100:.0f}%")
            hits = sum(1 for g in dir_rows if g["call"] == g["realized"])
            print(f"  HMM  {hits}/{len(dir_rows)} = {hits / len(dir_rows) * 100:.0f}% "
                  f"(on all {len(dir_rows)} days)")

    stale = [r for r in rows if (r.get("stale_sessions") or 0) >= 2]
    if stale:
        print(f"\nNOTE: {len(stale)}/{len(rows)} posteriors were fitted on a tape "
              f"2+ sessions stale.")

    if json_out:
        with open(json_out, "w") as f:
            json.dump({"ticker": ticker, "coverage": cov,
                       "direction": dir_rows, "llm": llm}, f, indent=2, default=str)
        print(f"\nwrote {json_out}")
    return 0


def _grade_llm(days: set[str], spx: list[tuple]) -> dict:
    """The LLM's forward calls, scored only on days the HMM also covered."""
    from app.db.connection import get_db

    with get_db() as db:
        rows = db.execute(
            "SELECT cycle_id, created_at, desk_data FROM shared_desk "
            "ORDER BY created_at ASC"
        ).fetchall()

    seen: set[str] = set()
    hits = n = 0
    for cycle_id, created_at, desk_data in rows:
        if cycle_id in seen:
            continue
        desk = desk_data if isinstance(desk_data, dict) else json.loads(desk_data or "{}")
        call = (desk.get("regime_classification") or {}).get("forward_call")
        if not isinstance(call, dict) or not call.get("spx_direction"):
            continue
        made_on = created_at.date() if hasattr(created_at, "date") else created_at
        if str(made_on) not in days:
            continue
        seen.add(cycle_id)
        move = _move_after(spx, made_on, HORIZON_DAYS)
        if move is None:
            continue
        realized = ("UP" if move > SPX_DEADBAND_PCT
                    else "DOWN" if move < -SPX_DEADBAND_PCT else "FLAT")
        n += 1
        hits += (call["spx_direction"] == realized)
    return {"hits": hits, "n": n}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", default="SPY")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="fit + store point-in-time posteriors for the last N sessions")
    ap.add_argument("--stride", type=int, default=1,
                    help="fit every Kth session during backfill (cheaper, lower n)")
    ap.add_argument("--grade", action="store_true", help="score what is stored")
    ap.add_argument("--compare", action="store_true",
                    help="also grade the LLM regime engine on the same days")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    if not args.backfill and not args.grade:
        ap.error("nothing to do: pass --backfill N and/or --grade")

    rc = 0
    if args.backfill:
        rc = backfill(args.backfill, args.ticker.upper(), max(1, args.stride))
    if args.grade and rc == 0:
        rc = grade(args.ticker.upper(), args.compare, args.json_out)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
