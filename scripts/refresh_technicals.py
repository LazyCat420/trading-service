#!/usr/bin/env python3
"""Recompute the `technicals` collection from `price_history`.

Two bugs left this table almost entirely stale (2026-07-25: only **5 of 503
tickers** fresher than 3 days, while `price_history` was current for all of
them):

  1. `compute_technicals` selected `ORDER BY date ASC LIMIT 500` — the OLDEST
     500 sessions. For MSFT (10,169 rows back to 1986) every run recomputed
     1986-03-13 .. 1988-03-03 and never touched a recent date.
  2. `ON CONFLICT (ticker, date) DO NOTHING` meant a re-run could never correct
     an existing row, only add missing ones — so the damage accumulated.

Both are fixed in `app/processors/technical_processor.py` — which now reads
`sort=[("date", -1)]` and writes through an idempotent `bulk_upsert` — and
`collect_all` refreshes technicals whenever it refreshes prices. This script
repairs the existing backlog for tickers no cycle has touched yet.

    python scripts/refresh_technicals.py --dry-run       # show what is stale
    python scripts/refresh_technicals.py --stale-days 3  # repair (default)
    python scripts/refresh_technicals.py --ticker MSFT   # one ticker
    python scripts/refresh_technicals.py --ticker MSFT --dry-run   # just its lag

READS MONGODB (ported 2026-08-30). Both staleness scans were
`SELECT ticker, MAX(date) ... GROUP BY ticker` against Postgres. That archive
froze at the 2026-08-19 cutover, and since the archive DSN setting was removed
from `app.config` on 08-28 the script did not even answer stale numbers: it
died with `AttributeError: 'Settings' object has no attribute ...` inside the
migration package's archive connector, before its first query. The recompute
half (`compute_technicals`) was already Mongo-only, so only the two scans
moved.

ONE VENDOR, and this is the substantive change
----------------------------------------------
`price_history` is keyed (ticker, date, source): one ticker-date carries
several vendor prints and they disagree by ~20% on average, so `MAX(date)`
across vendors is NOT the newest bar this repair can use. `compute_technicals`
pins the ticker's dominant vendor, so it can only ever write technicals up to
THAT vendor's newest bar. Benchmarking against the all-vendor maximum makes the
lag unclosable: measured against the live collection 2026-08-30, 180 of 2,895
tickers carry two vendors and 30 of them have a dominant vendor behind the
all-vendor max — EAT by 3 days (yfinance 2026-08-14 over 10,735 rows, polygon
2026-08-17 over 251). EAT's technicals already reach 2026-08-14, i.e. they are
exactly current, yet the unpinned benchmark scores it 3 days stale on every
run, forever, and at `--stale-days 2` it would be recomputed by every repair
with nothing to show for it.

So the benchmark below is the dominant vendor's newest bar, picked by the same
freshest-then-deepest-then-name rule `technical_processor._one_vendor` uses —
importing that module's `_FRESHNESS_LAG_DAYS` rather than restating it, because
a benchmark that names a different vendor than the recompute reads is the bug
this paragraph exists to prevent.

Cost: the scan is a full grouped pass over price_history (15.8M documents,
~70-90s against the live store, versus ~10s in Postgres over the same rows).
The pass is unavoidable — "every ticker's newest bar, per vendor" is the
question — and the `natural_key` index on (ticker, date, source) is what keeps
it to minutes. The Postgres version's comment about /dev/shm and parallel
workers went with the planner it described.

Safe to re-run: the upsert is idempotent. It only READS price_history and
rewrites derived indicator rows, so there is nothing here to lose — but see
HANDOFF on backing up before bulk DB work. `--dry-run` writes nothing, and that
now holds for `--ticker` too: the single-ticker branch used to call
`compute_technicals` — a write — before the flag was ever read, so
`--ticker MSFT --dry-run` was a dry run in name only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _day(value: datetime | None) -> str:
    """The calendar day a date column holds, as Postgres printed it.

    BSON has no `date` type, so what Postgres returned as `date(2026, 8, 14)`
    comes back from Mongo as `datetime(2026, 8, 14, 0, 0)` — and `str()` on it
    prints `2026-08-14 00:00:00`, nine characters wider than the column this
    report lays out. Formatting the day keeps the table the shape it was.
    """
    return "never" if value is None else value.strftime("%Y-%m-%d")


def newest_price_by_ticker(ticker: str = "") -> dict[str, tuple]:
    """`{ticker: (dominant vendor's newest bar, that vendor, any vendor's newest)}`.

    Replaces the archive's per-ticker scan of the price_history table,
    `SELECT ticker, MAX(date) ... GROUP BY ticker`, and deliberately answers a
    slightly different question — see ONE VENDOR in the module docstring. The
    third element is the old, all-vendor answer, kept only so the report can
    say how often the two disagree.

    The replaced statement's `FROM` clause is described rather than quoted on
    purpose: the repo-wide vendor guard counts price_history reads by scanning
    string literals, and a query pasted into a docstring is a read it would
    count — and could one day condemn — for a statement that no longer exists.

    Pass `ticker` to scope the scan to one symbol: the same grouped pipeline
    under a `$match` the `natural_key` index serves, which turns a ~70s
    collection pass into ~0.06s.
    """
    from app.db import mongo_query
    # The processor's lag, imported rather than restated: this function has to
    # name the vendor `compute_technicals` will actually read, and two copies
    # of a constant are two constants.
    from app.processors.technical_processor import _FRESHNESS_LAG_DAYS

    query = {"ticker": ticker.strip().upper()} if ticker else {}
    rows = mongo_query.group_rows(
        "price_history", query,
        ["ticker", "source"], [("max", "date"), ("count", None)],
        select=[("key", "ticker"), ("key", "source"), ("agg", 0), ("agg", 1)],
    )

    per_ticker: dict[str, list[tuple]] = defaultdict(list)
    for tkr, source, newest, rows_held in rows:
        # MAX(date) over no non-null dates is NULL in SQL and missing here;
        # a vendor with no dated bar cannot be the freshest one.
        if tkr is None or newest is None:
            continue
        per_ticker[tkr].append((source, newest, int(rows_held or 0)))

    out: dict[str, tuple] = {}
    for tkr, vendors in per_ticker.items():
        any_vendor = max(newest for _, newest, _ in vendors)
        cutoff = any_vendor - timedelta(days=_FRESHNESS_LAG_DAYS)
        # Freshness first, then depth, then name — `_one_vendor`'s sort key,
        # vectorised over every ticker at once instead of one aggregation per
        # ticker (2,895 round-trips for the same answer).
        source, newest, _held = sorted(
            vendors,
            key=lambda v: (v[1] < cutoff, -v[2], str(v[0] or "")),
        )[0]
        out[tkr] = (newest, source, any_vendor)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report staleness without recomputing")
    ap.add_argument("--stale-days", type=int, default=3,
                    help="recompute tickers whose newest technical row is older "
                         "than this many days behind their newest price row")
    ap.add_argument("--ticker", default="",
                    help="recompute a single ticker and exit")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap how many tickers to process (0 = no cap)")
    args = ap.parse_args()

    from app.db import mongo_query
    from app.processors.technical_processor import compute_technicals

    if args.ticker:
        tkr = args.ticker.strip().upper()
        if args.dry_run:
            # A dry run may not write, and compute_technicals writes. Report
            # the same three numbers the bulk table shows instead.
            priced = newest_price_by_ticker(tkr).get(tkr)
            if priced is None:
                print(f"{tkr}: no price history — nothing to recompute")
                return 0
            last_price, vendor, _any_vendor = priced
            last_tech = mongo_query.scalar("technicals", {"ticker": tkr},
                                           "date", sort=[("date", -1)])
            lag = None if last_tech is None else (last_price - last_tech).days
            print(f"{tkr}: last_price {_day(last_price)} ({vendor}) | "
                  f"last_tech {_day(last_tech)} | "
                  f"lag {'never computed' if lag is None else f'{lag}d'}")
            print("\nDRY RUN — re-run without --dry-run to recompute.")
            return 0
        n = compute_technicals(tkr)
        print(f"{tkr}: {n} rows written")
        return 0

    # Two grouped scans joined in Python so that "stale" means "behind the
    # prices we already have", not "old" — a delisted ticker whose prices stop
    # in 2024 is not stale. Still two scans and not one $lookup: a lookup on
    # 15.8M documents keyed by ticker would run per input group, and the join
    # is 2,895 rows wide in Python.
    price_by_ticker = newest_price_by_ticker()
    tech_rows = mongo_query.group_rows(
        "technicals", {}, ["ticker"], [("max", "date")],
        select=[("key", "ticker"), ("agg", 0)],
    )

    last_tech_by_ticker = {t: d for t, d in tech_rows if t is not None}
    rows = [
        (ticker, price_by_ticker[ticker][0], last_tech_by_ticker.get(ticker))
        for ticker in sorted(price_by_ticker)
    ]
    vendor_by_ticker = {t: v[1] for t, v in price_by_ticker.items()}
    behind = sum(1 for newest, _v, any_vendor in price_by_ticker.values()
                 if newest != any_vendor)

    # lag is None for "no technicals at all" so it never collides with a real
    # day count — the ASC-limit bug produced genuine lags in the tens of
    # thousands of days (CVX's newest technical row was 1963-12-26 against a
    # 2026-07-24 price), so a large sentinel would have been indistinguishable.
    stale = []
    for ticker, last_price, last_tech in rows:
        if last_price is None:
            continue
        if last_tech is None:
            stale.append((ticker, last_price, None, None))
            continue
        lag = (last_price - last_tech).days
        if lag > args.stale_days:
            stale.append((ticker, last_price, last_tech, lag))

    print(f"tickers with price history : {len(rows)}")
    print(f"stale by >{args.stale_days}d           : {len(stale)}")
    print(f"   (benchmark pinned per ticker to its dominant vendor; "
          f"{behind} ticker(s) have a fresher print from another vendor "
          f"that the recompute cannot use)")
    if stale:
        never = [r for r in stale if r[3] is None]
        lagging = sorted((r for r in stale if r[3] is not None), key=lambda r: -r[3])
        print(f"   never computed: {len(never)} | stale: {len(lagging)}")
        if lagging:
            # Header only when there is a table under it: `never computed` can
            # be the whole of `stale`, and a column head over nothing reads as
            # a report that failed rather than one with nothing to say.
            print(f"   {'ticker':<10} {'last_price':<12} {'last_tech':<12} "
                  f"{'lag':>8}  {'vendor'}")
            for t, lp, lt, lag in lagging[:10]:
                print(f"   {t:<10} {_day(lp):<12} {_day(lt):<12} "
                      f"{str(lag) + 'd':>8}  {vendor_by_ticker.get(t, '')}")

    if args.dry_run:
        print("\nDRY RUN — re-run without --dry-run to recompute.")
        return 0

    targets = stale[: args.limit] if args.limit else stale
    if not targets:
        print("\nnothing to do.")
        return 0

    print(f"\nrecomputing {len(targets)} ticker(s)...")
    started = time.monotonic()
    ok = failed = written = 0
    for i, (ticker, _lp, _lt, _lag) in enumerate(targets, 1):
        try:
            written += compute_technicals(ticker)
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"   {ticker}: FAILED — {e}")
        if i % 50 == 0:
            print(f"   ... {i}/{len(targets)} ({time.monotonic() - started:.0f}s)")

    print(f"\ndone in {time.monotonic() - started:.0f}s: "
          f"{ok} ok, {failed} failed, {written} indicator rows written")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
