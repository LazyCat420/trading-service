#!/usr/bin/env python3
"""Recompute the `technicals` table from `price_history`.

Two bugs left this table almost entirely stale (2026-07-25: only **5 of 503
tickers** fresher than 3 days, while `price_history` was current for all of
them):

  1. `compute_technicals` selected `ORDER BY date ASC LIMIT 500` — the OLDEST
     500 sessions. For MSFT (10,169 rows back to 1986) every run recomputed
     1986-03-13 .. 1988-03-03 and never touched a recent date.
  2. `ON CONFLICT (ticker, date) DO NOTHING` meant a re-run could never correct
     an existing row, only add missing ones — so the damage accumulated.

Both are fixed in `app/processors/technical_processor.py`, and `collect_all`
now refreshes technicals whenever it refreshes prices. This script repairs the
existing backlog for tickers no cycle has touched yet.

    python scripts/refresh_technicals.py --dry-run       # show what is stale
    python scripts/refresh_technicals.py --stale-days 3  # repair (default)
    python scripts/refresh_technicals.py --ticker MSFT   # one ticker

Safe to re-run: the upsert is idempotent. It only READS price_history and
rewrites derived indicator rows, so there is nothing here to lose — but see
HANDOFF on backing up before bulk DB work.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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

    from scripts.migration.pg_connection import get_db
    from app.processors.technical_processor import compute_technicals

    if args.ticker:
        n = compute_technicals(args.ticker.strip().upper())
        print(f"{args.ticker.upper()}: {n} rows written")
        return 0

    # Join the two tables so "stale" means "behind the prices we already have",
    # not "old" — a delisted ticker whose prices stop in 2024 is not stale.
    #
    # Deliberately two grouped scans joined in Python rather than one SQL JOIN:
    # price_history is ~1.9M rows and the joined aggregate makes the planner
    # fan out to parallel workers, which blows the postgres container's default
    # 64MB /dev/shm ("could not resize shared memory segment"). The DB
    # container is not ours to reconfigure, so the query is shaped to avoid
    # needing the extra memory.
    with get_db() as db:
        price_rows = db.execute(
            "SELECT ticker, MAX(date) FROM price_history GROUP BY ticker"
        ).fetchall()
        tech_rows = db.execute(
            "SELECT ticker, MAX(date) FROM technicals GROUP BY ticker"
        ).fetchall()

    last_tech_by_ticker = {t: d for t, d in tech_rows}
    rows = [
        (ticker, last_price, last_tech_by_ticker.get(ticker))
        for ticker, last_price in sorted(price_rows)
    ]

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
    if stale:
        never = [r for r in stale if r[3] is None]
        lagging = sorted((r for r in stale if r[3] is not None), key=lambda r: -r[3])
        print(f"   never computed: {len(never)} | stale: {len(lagging)}")
        print(f"   {'ticker':<10} {'last_price':<12} {'last_tech':<12} {'lag':>8}")
        for t, lp, lt, lag in lagging[:10]:
            print(f"   {t:<10} {str(lp):<12} {str(lt):<12} {str(lag) + 'd':>8}")

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
