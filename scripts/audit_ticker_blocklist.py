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

WHICH STORE THE DEPTH HALF READS. MongoDB, since 2026-08-30. It read Postgres
until then, and that is worth writing down because of HOW it failed: Postgres
stopped taking writes at the 2026-08-19 cutover, so the depth section kept
running clean and kept printing the archive's frozen numbers as if they were
today's. Measured side by side the day of the port, the two stores answer
different questions with the same sentence — the archive says the median depth
of `price_history` is 4,769 bars over 2,886 tickers, the live store says 4,743
over 2,895, and 22 of the 123 audited symbols have gained sessions since the
cutover that the archive cannot see. A depth audit that reads a frozen store is
not stale by a little; it is measuring a different collection.

USAGE
    python3 scripts/audit_ticker_blocklist.py            # fetch + compare
    python3 scripts/audit_ticker_blocklist.py --offline DIR
        # use nasdaqlisted.txt / otherlisted.txt already in DIR
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NASDAQ = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

#: Concurrency for the per-ticker depth scan. One `distinct` per ticker costs
#: ~80 ms against the live store and there are ~2,900 of them, so serially the
#: scan takes ~230 s against the ~39 s the single Postgres GROUP BY took. Eight
#: workers bring it back to ~53 s. Each call is an independent indexed read on
#: `(ticker, date, source)`; nothing here writes.
_DEPTH_WORKERS = 8


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


def percentile_disc(values: Iterable[int], fraction: float = 0.5) -> Optional[int]:
    """Postgres `percentile_disc(fraction) WITHIN GROUP (ORDER BY v)`, in Python.

    DISCRETE, and the distinction is not pedantic. `percentile_disc` returns a
    value that is actually IN the set — for an even-sized set, the lower of the
    two middles — while `statistics.median` averages them. On the 2,886-ticker
    archive the two answers are 4,769 and 4,771.5. The number is printed as a
    bar count and used as a `median / 4` threshold against real bar counts, so
    the member of the set is the one that means anything.

    Returns None for an empty input, which is what Postgres returns (NULL) when
    the ordered set is empty; the caller must not format it as a number.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    # ceil(fraction * N) in 1-based terms — the first element whose cumulative
    # distribution reaches `fraction`. Verified equal to Postgres on the live
    # archive: 2,886 tickers, both 4,769.
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def distinct_dates(ticker: str) -> int:
    """`SELECT count(DISTINCT date) FROM price_history WHERE ticker = %s`.

    DISTINCT dates, never a row count: `price_history`'s natural key is
    `(ticker, date, source)`, so one session can carry several vendor prints
    and a raw `count(*)` reports vendors, not history. This script's first
    draft had exactly that flaw and `test_price_history_one_vendor_guard`
    caught it — the depth numbers it reported were inflated.

    And deliberately NOT pinned to one vendor, which is the other half of the
    same rule. A distinct set of DATES cannot be inflated by a duplicate print
    of a session it already contains, so the count is vendor-immune by
    construction; pinning a vendor here would silently narrow "how much history
    does price_history hold for this symbol" to "how much of it did yfinance
    publish", which is not the sentence printed above the numbers. The vendor
    guard recognises this shape — see its `distinct_values` exemption.
    """
    from app.db import mongo_store

    return len(mongo_store.distinct_values("price_history", "date", {"ticker": ticker}))


def depth_by_ticker() -> dict[str, int]:
    """`SELECT ticker, count(DISTINCT date) FROM price_history GROUP BY ticker`.

    The whole distribution, because the median is an order statistic: there is
    no sampling shortcut that still answers the question the next line prints.
    A ticker absent from the result has no rows at all, exactly as GROUP BY
    omitted it.
    """
    from app.db import mongo_store

    tickers = mongo_store.distinct_values("price_history", "ticker")
    # stderr, so the report on stdout keeps byte-for-byte the shape it had.
    print(f"(scanning price_history depth for {len(tickers):,} tickers…)",
          file=sys.stderr)
    with ThreadPoolExecutor(max_workers=_DEPTH_WORKERS) as pool:
        counts = list(pool.map(distinct_dates, tickers))
    return dict(zip(tickers, counts))


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
        depth = depth_by_ticker()
    except Exception as e:  # noqa: BLE001
        print(f"\n(depth check skipped — database unreachable: {e})")
        return 0

    median = percentile_disc(depth.values())
    if median is None:
        # Postgres returned NULL here and the old code formatted it as a
        # number, so an empty price_history raised TypeError instead of saying
        # so. Say so.
        print("\n(depth check skipped — price_history holds no rows)")
        return 0

    # ORDER BY 2 DESC, with the ticker as a tiebreak so two symbols on the same
    # bar count do not swap places between runs and read as a change.
    rows = sorted(((t, depth[t]) for t in sorted(fresh) if t in depth),
                  key=lambda r: (-r[1], r[0]))

    print(f"\nDEPTH — median across all of price_history is {median:,} bars.")
    print("Freshness passes for all of these; depth is what is missing.")
    for t, n in rows:
        print(f"  {t:<6} {n:>6} bars  {'← under a quarter of median' if n < median / 4 else ''}")
    print(f"  {len(fresh) - len(rows)} of {len(fresh)} have no rows at all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
