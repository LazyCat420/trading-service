#!/usr/bin/env python3
"""Re-derive the slang blocklist's listed/unlisted split against a listings source.

WHY THIS EXISTS. `explicit_fetch_guard.LISTED_ON_THE_SLANG_LIST` is a snapshot,
and a snapshot has no invalidation of its own. This is the invalidation: it
fetches the NASDAQ Trader symbol directory, re-derives the set, and prints the
difference. Run it when a name that should be fetchable is not being fetched,
or periodically.

It also prints the depth damage, because that is the part a freshness check
cannot see — other vendors backfill the recent tip, so `last_price` reads today
for a symbol carrying 48 bars against a ~4,800-bar median.

READ-ONLY. It prints a diff and never edits the guard; adding a symbol to the
allowlist should be a reviewable commit, not a script's side effect.

USAGE
    python3 scripts/audit_ticker_blocklist.py            # fetch + compare
    python3 scripts/audit_ticker_blocklist.py --offline DIR
        # use nasdaqlisted.txt / otherlisted.txt already in DIR
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NASDAQ = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"


def _parse(text: str, *keys: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="|"):
        sym = next((row[k].strip() for k in keys if row.get(k)), "")
        # The directory's last line is a "File Creation Time" trailer, not a
        # symbol. Left in, it becomes a phantom listing.
        if sym and not sym.startswith("File Creation"):
            out.setdefault(sym, (row.get("Security Name") or "").strip())
    return out


def load_listings(offline: Path | None) -> dict[str, str]:
    if offline:
        a = _parse((offline / "nasdaqlisted.txt").read_text(), "Symbol")
        b = _parse((offline / "otherlisted.txt").read_text(), "ACT Symbol", "NASDAQ Symbol")
    else:
        import httpx

        with httpx.Client(timeout=30.0) as c:
            a = _parse(c.get(NASDAQ).text, "Symbol")
            b = _parse(c.get(OTHER).text, "ACT Symbol", "NASDAQ Symbol")
    return {**b, **a}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", type=Path, help="directory holding the two .txt files")
    args = ap.parse_args()

    from app.collectors.explicit_fetch_guard import (
        LISTED_ON_THE_SLANG_LIST,
        LISTED_SNAPSHOT_DATE,
        explicit_fetch_blocklist,
    )
    from app.processors.ticker_extractor import STATIC_FALSE_TICKERS

    listings = load_listings(args.offline)
    print(f"listings source: {len(listings):,} currently listed US symbols")
    print(f"slang list:      {len(STATIC_FALSE_TICKERS)} entries")
    print(f"snapshot dated:  {LISTED_SNAPSHOT_DATE} ({len(LISTED_ON_THE_SLANG_LIST)} listed)\n")

    fresh = {t for t in STATIC_FALSE_TICKERS if t in listings}

    newly_listed = sorted(fresh - LISTED_ON_THE_SLANG_LIST)
    no_longer = sorted(LISTED_ON_THE_SLANG_LIST - fresh)

    if newly_listed:
        print("LISTED SINCE THE SNAPSHOT — currently being refused on the explicit path:")
        for t in newly_listed:
            print(f"  {t:<6} {listings[t][:70]}")
        print("  → add these to LISTED_ON_THE_SLANG_LIST\n")
    if no_longer:
        print("NO LONGER LISTED — the allowlist entry is now unnecessary:")
        print(f"  {no_longer}")
        print("  → harmless, but removing them restores the skip-not-outage path\n")
    if not newly_listed and not no_longer:
        print("Snapshot is current — no symbols have changed listing status.\n")

    print(f"explicit-fetch blocklist is {len(explicit_fetch_blocklist())} entries "
          f"(slang minus listed)")

    try:
        from scripts.migration.pg_connection import get_db

        with get_db() as db:
            median = db.execute(
                "SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY n) FROM "
                # count(DISTINCT date), never count(*): price_history mixes
                # vendor conventions and two vendors printing the same session
                # inflate a raw count. `test_price_history_one_vendor_guard`
                # enforces this and caught exactly that flaw in this script's
                # first draft — the depth numbers it reported were inflated.
                "(SELECT count(DISTINCT date) AS n FROM price_history GROUP BY ticker) s"
            ).fetchone()[0]
            rows = db.execute(
                "SELECT ticker, count(DISTINCT date) FROM price_history "
                "WHERE ticker = ANY(%s) GROUP BY ticker ORDER BY 2 DESC",
                [sorted(fresh)],
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"\n(depth check skipped — database unreachable: {e})")
        return 0

    print(f"\nDEPTH — median across all of price_history is {median:,} bars.")
    print("Freshness passes for all of these; depth is what is missing.")
    have = dict(rows)
    for t, n in rows:
        print(f"  {t:<6} {n:>6} bars  {'← under a quarter of median' if n < median / 4 else ''}")
    print(f"  {len(fresh) - len(have)} of {len(fresh)} have no rows at all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
