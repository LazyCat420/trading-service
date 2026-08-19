#!/usr/bin/env python3
"""Rebuild `technicals` from Mongo `price_history`. NOT a migration.

WHY THIS IS NOT BACKFILLED
--------------------------
`technicals` is 1.37M rows of DERIVED data — RSI, MACD, Bollinger bands and so
on, all computed from `price_history` by `technical_processor.compute_technicals`.
Copying it out of Postgres would migrate the OUTPUT of a computation whose input
is already being migrated, and would carry across whatever the Postgres rows
happen to contain: indicators computed from a vendor mix, rows from a window
that has since changed, values from a `ta` version nobody recorded. Recomputing
gets a table that agrees with the price history under it, which is the only
property anything reads it for.

It is also cheap in the way that matters: nothing about the recompute needs the
old rows, so it can run AFTER the cutover, at leisure, while the desk reads what
it has produced so far.

COVERAGE, NOT ROW COUNT
-----------------------
The pass criterion is per-ticker coverage against `price_history`, not equality
with Postgres's 1,370,360 rows. `compute_technicals(period=N)` reads the last N
sessions and drops the first 13 (RSI's warm-up), so the row count is a function
of the window, and the window is a choice. `--verify` reports coverage; the row
count is printed for information and is expected to land within a few percent of
the Postgres figure at the default period.

    scripts/recompute_technicals.py --dry-run
    scripts/recompute_technicals.py                     # every eligible ticker
    scripts/recompute_technicals.py --tickers AAPL MSFT
    scripts/recompute_technicals.py --verify            # coverage only, no writes
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mongo_store  # noqa: E402
from app.processors.technical_processor import _MIN_SESSIONS, compute_technicals  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"


def _price_coverage() -> dict[str, int]:
    """`{ticker: session_count}` from price_history, ONE vendor per ticker.

    Counting all rows would double-count a ticker carried by two vendors and
    make a fully-covered ticker look under-covered by half. The dominant vendor
    per ticker is what `compute_technicals` itself reads, so the coverage
    denominator is measured the same way the numerator is produced.
    """
    rows = mongo_store.aggregate("price_history", [
        {"$group": {"_id": {"ticker": "$ticker", "source": "$source"},
                    "n": {"$sum": 1}}},
    ])
    best: dict[str, int] = {}
    for r in rows:
        t = (r["_id"] or {}).get("ticker")
        if not t:
            continue
        best[t] = max(best.get(t, 0), int(r["n"]))
    return best


def _technicals_coverage() -> dict[str, int]:
    rows = mongo_store.aggregate("technicals", [
        {"$group": {"_id": "$ticker", "n": {"$sum": 1}}},
    ])
    return {r["_id"]: int(r["n"]) for r in rows if r.get("_id")}


def verify(period: int) -> int:
    price, tech = _price_coverage(), _technicals_coverage()
    eligible = {t: n for t, n in price.items() if n >= _MIN_SESSIONS}
    # What a full pass can produce for this ticker: the window, capped by the
    # history available, minus RSI's warm-up.
    expected = {t: max(0, min(period, n) - (_MIN_SESSIONS - 1)) for t, n in eligible.items()}

    missing = sorted(t for t in eligible if t not in tech)
    short = sorted(
        (t for t in eligible if t in tech and tech[t] < expected[t] * 0.95),
        key=lambda t: expected[t] - tech[t], reverse=True)
    extra = sorted(t for t in tech if t not in price)

    print(f"price_history: {len(price):,} tickers ({len(eligible):,} with "
          f">= {_MIN_SESSIONS} sessions)")
    print(f"technicals:    {len(tech):,} tickers, {sum(tech.values()):,} rows")
    print(f"  no technicals at all:      {len(missing):,}")
    print(f"  under 95% of the window:   {len(short):,}")
    print(f"  technicals with no prices: {len(extra):,}")
    for t in missing[:10]:
        print(f"    MISSING {t} ({eligible[t]} sessions available)")
    for t in short[:10]:
        print(f"    SHORT   {t} {tech[t]}/{expected[t]}")
    for t in extra[:10]:
        print(f"    ORPHAN  {t} ({tech[t]} rows, no price history)")

    ok = not missing and not short
    print("\nCOVERAGE OK" if ok else "\nCOVERAGE INCOMPLETE")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="*", help="restrict to these tickers")
    ap.add_argument("--period", type=int, default=500,
                    help="sessions read per ticker (default 500, as the cycle uses)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N tickers")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="report coverage against price_history and exit")
    ap.add_argument("--json", type=Path, default=REPORTS / "recompute_technicals.json")
    args = ap.parse_args()

    if args.verify:
        return verify(args.period)

    coverage = _price_coverage()
    eligible = sorted(t for t, n in coverage.items() if n >= _MIN_SESSIONS)
    if args.tickers:
        wanted = {t.upper() for t in args.tickers}
        # A ticker asked for and NOT eligible is reported, not silently dropped:
        # "I ran it and nothing happened" is the failure mode of a filter that
        # says nothing.
        for t in sorted(wanted - set(eligible)):
            have = coverage.get(t, 0)
            print(f"skipping {t}: {have} session(s), needs >= {_MIN_SESSIONS}")
        eligible = [t for t in eligible if t in wanted]
    if args.limit:
        eligible = eligible[: args.limit]

    print(f"{len(eligible):,} eligible ticker(s), period={args.period}")
    if args.dry_run:
        for t in eligible[:20]:
            print(f"  {t:8} {coverage[t]:>6,} sessions")
        if len(eligible) > 20:
            print(f"  … {len(eligible) - 20:,} more")
        return 0

    started = time.time()
    written = 0
    failures: list[dict] = []
    empty: list[str] = []
    for i, ticker in enumerate(eligible, 1):
        try:
            n = compute_technicals(ticker, period=args.period)
        except Exception as exc:  # noqa: BLE001 - one ticker must not end the run
            failures.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
            print(f"[{i}/{len(eligible)}] {ticker}: ERROR {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        written += n
        if n == 0:
            empty.append(ticker)
        if i % 100 == 0 or i == len(eligible):
            rate = written / max(1e-9, time.time() - started)
            print(f"[{i}/{len(eligible)}] {written:,} rows  {rate:,.0f} rows/s")

    elapsed = time.time() - started
    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "tickers_attempted": len(eligible),
        "tickers_empty": empty,
        "rows_written": written,
        "wall_seconds": round(elapsed, 1),
        "failures": failures,
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=1))
    print(f"\n{written:,} rows across {len(eligible):,} tickers in "
          f"{elapsed / 60:.1f} min; {len(failures)} failure(s), "
          f"{len(empty)} produced nothing")
    print(f"wrote {args.json}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
