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

READS MONGO, since 2026-08-30
-----------------------------
It read PostgreSQL through `scripts.migration.pg_connection`, and PostgreSQL was
retired at the 2026-08-19 cutover. This one did not go quiet — it went LOUD:
`settings.DATABASE_URL` no longer exists, so every invocation died with
`AttributeError: 'Settings' object has no attribute 'DATABASE_URL'`, exit 1,
zero output. Dead either way.

Three things a mechanical port of THIS file gets wrong, each measured against
the live stores on 2026-08-30:

1. `fill_price`, `fill_value` and `fees` are money (`app/db/money_policy.py`)
   and read back as `Decimal`; `decision_price` is NOT on that list and reads
   back as `float`. The subtraction at the heart of this script therefore
   raises `TypeError: unsupported operand type(s) for -: 'decimal.Decimal' and
   'float'` on the FIRST priced fill. Promoted with `as_money()`, never by
   demoting the Decimal — see the rule in `mongo_query.as_money`. (The write
   side already treats it as money: `paper_trader` stores it via
   `mongo_store.dec128()`. The policy list is what disagrees.)

2. `filled_at::date` is a computed column — `scripts/sql_to_mongo.translate()`
   refuses the statement for exactly that reason ("SELECT item Cast is a
   computed column — compute it in Python after the find_docs()"). The date is
   taken off the timestamp here, and the timestamp is what the query sorts on.

3. `--since` is text. `{"$gte": "2026-01-01"}` against a BSON Date is a
   cross-type comparison that matches nothing; it survives today only because
   `mongo_store` routes every filter through `date_fields.coerce_filter`, which
   knows `trade_fills.filled_at` is a timestamp. That is a seam this script does
   not own, so the parse happens here.

THE ONE PLACE THIS DELIBERATELY DISAGREES WITH THE ARCHIVE
----------------------------------------------------------
The old statement ended `... filled_at::date FROM trade_fills ... ORDER BY
filled_at DESC`, and the cast's output column is ALSO named `filled_at`. SQL
resolves an unqualified ORDER BY name against the SELECT LIST FIRST, so the
report was sorted by the **calendar date**, not the timestamp — every fill
sharing a day was an unbroken tie in arbitrary order. Postgres says so out
loud the moment both columns are visible: `ORDER BY "filled_at" is ambiguous`.
Measured 2026-08-30 on the twelve priced fills, `ORDER BY filled_at` returned
GOOG AMZN **ET TRMB SE** FCF, while `ORDER BY trade_fills.filled_at` returned
GOOG AMZN **TRMB SE ET** FCF — 20:55, 07:45 and 05:24 on 2026-08-12.

This sorts on the timestamp, which is what "newest fill first" means and what
the qualified form returns. The totals are unaffected (a sum does not depend on
row order): both orders give +3.63 bps on $32,661.52 with $11.86 of fees.

State of the ledger on 2026-08-30: `trade_fills` holds 56 documents, the same 56
the frozen archive holds, because the book has taken NO fill since 2026-08-18
14:39 — `orders` is frozen at the same instant. 44 of the 56 predate 2026-07-26
and carry no `decision_price` and zero fees; the 12 that do are all `test_bot`.
So an empty post-cutover report here is the BOOK being quiet, not this
instrument being broken, and it says which.

Usage:
    python scripts/execution_quality.py
    python scripts/execution_quality.py --since 2026-07-01
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mongo_query  # noqa: E402
from app.db.mongo_query import as_money  # noqa: E402

# A POSTGRES TABLE NAME, never a resolved collection name: every mongo_query
# helper calls collection_for() internally, exactly once.
FILLS = "trade_fills"

# The SELECT list, in order, minus the `::date` cast — that is computed below.
COLUMNS = ["ticker", "side", "fill_qty", "fill_price", "decision_price",
           "fill_value", "fees", "filled_at"]

BPS = Decimal(10_000)


def parse_since(text: str) -> datetime:
    """`WHERE filled_at >= %s` with a text argument, as Postgres read it.

    Postgres parsed the string into a timestamp itself. Mongo does not: string
    and Date are different BSON types and `$gte` does not compare across them,
    so an unparsed `--since` is a filter that silently matches nothing.
    """
    try:
        return datetime.fromisoformat(str(text))
    except ValueError as exc:
        # Postgres answered an unparseable argument with a psycopg
        # InvalidDatetimeFormat traceback and exit 1. Same exit code, one line.
        raise ValueError(f"--since {text!r} is not a date or timestamp: {exc}") from exc


def load_fills(since: datetime) -> list[tuple]:
    """The rows the SQL returned, in the same order, as tuples in SELECT order.

    `filled_at` replaces `filled_at::date` in the projection because the sort
    key must keep its time-of-day. Five of the twelve priced fills share a
    calendar day with another, and the archive's `ORDER BY filled_at` bound to
    the CAST rather than the column, so it ordered those five arbitrarily — see
    the docstring above. Sorting the timestamp is the fix, not a drift.
    """
    rows = mongo_query.find_rows(FILLS, {"filled_at": {"$gte": since}}, COLUMNS,
                                 sort=[("filled_at", -1)])
    return [r[:7] + (as_day(r[7]),) for r in rows]


def as_day(value):
    """`filled_at::date`. A string timestamp is not silently accepted here —
    it cannot reach this point, because the `$gte` above would not have matched
    it (see `unreadable_fills`)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date) or value is None:
        return value
    return str(value)[:10]


def unreadable_fills() -> int:
    """Fills the window filter CANNOT see: no `filled_at`, or one stored as text.

    Postgres declared `filled_at` NOT NULL and no such row can exist in the
    archive. Mongo has no such constraint, and `$gte` matches neither a missing
    field nor a string, so a fill written without a real timestamp would be
    invisible to every window this script offers — silently, and with no row
    count looking wrong. Counted so an empty report can say which kind of empty
    it is (0 of 56 on 2026-08-30).
    """
    return mongo_query.count(FILLS) - mongo_query.count(
        FILLS, {"filled_at": {"$type": "date"}})


def shortfall_bps(side, fill_price, decision_price) -> Decimal:
    """Signed so POSITIVE always means "worse than the decision price".

    A buy filling high and a sell filling low are the same failure and must not
    cancel each other in the average.

    `as_money` on BOTH prices: `fill_price` arrives as `Decimal` (it is money)
    and `decision_price` as `float` (it is not, per `money_policy`), and mixing
    them raises TypeError. Promoting the float is the direction that keeps the
    exactness the money column was promoted for; demoting the Decimal throws it
    away at the one site that does the arithmetic.
    """
    fill = as_money(fill_price)
    decision = as_money(decision_price)
    raw = (fill - decision) / decision
    return (raw if str(side).upper() == "BUY" else -raw) * BPS


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Kept as text for the headings, which print it exactly as typed; the
    # datetime the filter needs is derived, not substituted.
    ap.add_argument("--since", default="2026-01-01")
    args = ap.parse_args()
    try:
        since = parse_since(args.since)
    except ValueError as exc:
        print(f"execution_quality: {exc}", file=sys.stderr)
        return 1

    rows = load_fills(since)

    invisible = unreadable_fills()
    if invisible:
        print(f"⚠ {invisible} fill(s) carry no usable `filled_at` and are "
              f"invisible to every\n  window this report can ask for. They are "
              f"NOT in the counts below.")

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

    total_shortfall_bps = Decimal(0)
    total_fees = Decimal(0)
    total_value = Decimal(0)
    for ticker, side, qty, fill_price, decision_price, value, fees, day in priced:
        bps = shortfall_bps(side, fill_price, decision_price)
        # Value-WEIGHTED, not a mean of the column: a 4-share fill and a
        # 4,000-share fill cost the book very different amounts of money for the
        # same bps, and the unweighted average of the two is not a cost.
        amount = as_money(value or 0)
        total_shortfall_bps += bps * amount
        total_fees += as_money(fees or 0)
        total_value += amount
        print(f"{ticker:8}{side:6}{float(qty):>10.3f}{as_money(decision_price):>11.4f}"
              f"{as_money(fill_price):>11.4f}{bps:>9.2f}{as_money(fees or 0):>10.4f}  {day}")

    weighted = total_shortfall_bps / total_value if total_value else Decimal(0)
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
