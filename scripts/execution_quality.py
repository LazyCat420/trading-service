#!/usr/bin/env python3
"""Realized implementation shortfall — what execution actually cost the book.

Perold (1988): the gap between the price a decision was made at and the price it
was filled at. Before 2026-07-26 this was identically zero here by construction —
`paper_trader` filled at exactly the reference close with `fees = 0` — so every
performance number the service produced was gross of all friction.

    IS = (fill_price - decision_price) / decision_price, signed so that a
         positive number always means the trade was WORSE than the decision price

This is the feedback loop that keeps `app/quant/execution_costs.py` honest. That
module MODELS costs from ADV liquidity tiers; this one reports what the ledger
actually recorded. When they diverge, the model is wrong and should be
recalibrated — a modeled cost presented as a measured one is exactly the
laundering this codebase keeps finding.

Caveat, stated plainly: on a paper book the fill price is *derived from* the same
cost model, so IS here currently measures the model's own output rather than
market reality. It becomes a genuine independent check only against a real
broker. Until then its job is narrower but still real: proving costs are being
applied at all, with the right sign, and in the right size.

Usage:
    python scripts/execution_quality.py
    python scripts/execution_quality.py --since 2026-07-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.migration.pg_connection import get_db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-01-01")
    args = ap.parse_args()

    with get_db() as db:
        rows = db.execute(
            """
            SELECT ticker, side, fill_qty, fill_price, decision_price,
                   fill_value, fees, filled_at::date
            FROM trade_fills
            WHERE filled_at >= %s
            ORDER BY filled_at DESC
            """,
            [args.since],
        ).fetchall()

    if not rows:
        print(f"No fills since {args.since}.")
        return 0

    priced = [r for r in rows if r[4] and float(r[4]) > 0]
    unpriced = len(rows) - len(priced)

    print("=" * 84)
    print(f"EXECUTION QUALITY — {len(rows)} fills since {args.since}")
    print("=" * 84)

    if unpriced:
        # Not a defect: fills before the decision_price column existed were
        # genuinely frictionless. Saying so beats reporting a 0bp shortfall that
        # would read as "execution was free".
        print(f"\n{unpriced} fill(s) carry no decision_price — recorded before "
              f"2026-07-26,\nwhen fills happened at exactly the reference price. "
              f"Excluded, not counted as zero-cost.")

    if not priced:
        print("\nNo cost-bearing fills yet. Trade once on the new build and re-run.")
        return 0

    print(f"\n{'ticker':8}{'side':6}{'qty':>10}{'decision':>11}{'fill':>11}"
          f"{'IS bps':>9}{'fees':>10}  date")
    print("-" * 84)

    total_shortfall_bps = 0.0
    total_fees = 0.0
    total_value = 0.0
    for ticker, side, qty, fill_price, decision_price, value, fees, day in priced:
        fill_price = float(fill_price)
        decision_price = float(decision_price)
        # Signed so POSITIVE always means "worse than the decision price",
        # whichever side we were on. A buy filling high and a sell filling low
        # are the same failure and must not cancel each other in the average.
        raw = (fill_price - decision_price) / decision_price
        shortfall_bps = (raw if str(side).upper() == "BUY" else -raw) * 10_000.0
        total_shortfall_bps += shortfall_bps * float(value or 0)
        total_fees += float(fees or 0)
        total_value += float(value or 0)
        print(f"{ticker:8}{side:6}{float(qty):>10.3f}{decision_price:>11.4f}"
              f"{fill_price:>11.4f}{shortfall_bps:>9.2f}{float(fees or 0):>10.4f}  {day}")

    weighted = total_shortfall_bps / total_value if total_value else 0.0
    print("-" * 84)
    print(f"\nValue-weighted implementation shortfall: {weighted:+.2f} bps")
    print(f"Total fees recorded: ${total_fees:,.2f} on ${total_value:,.2f} traded")

    if weighted < 0:
        print("\n⚠ NEGATIVE shortfall means fills were BETTER than the decision "
              "price.\n  On a paper book that is not price improvement — it is a "
              "sign error in the\n  cost model, and it would make every strategy "
              "look better the more it traded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
