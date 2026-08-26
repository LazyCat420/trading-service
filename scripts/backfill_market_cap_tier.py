"""Backfill ticker_metadata.market_cap_tier for names the mega-cap gate is blind to.

WHY (ch.97, 2026-08-25): the "maximum one mega-cap per cycle" rule tests
`ticker_metadata.market_cap_tier == "mega"`, but the tier is only written by
`load_sp500_universe(enrich=True)` — and boot always runs `enrich=False`, so
510 of 1,049 rows (AAPL and TSLA among them) carried no tier at all. The
enforcement shipped; the data it reads never did.

Reads market caps from yfinance in batches, maps them through the same
`tier_for_market_cap` the loader uses, and updates ONLY rows whose tier is
missing (never overwrites an existing tier — `--force` to re-tier everything).

Usage (inside the trading-service container, or anywhere with MONGO_URI):
    python scripts/backfill_market_cap_tier.py            # missing tiers only
    python scripts/backfill_market_cap_tier.py --dry-run  # report, write nothing
    python scripts/backfill_market_cap_tier.py --only AAPL TSLA
    python scripts/backfill_market_cap_tier.py --force    # re-tier all rows
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from app.data.sp500_universe import tier_for_market_cap  # noqa: E402
from app.db import mongo_store  # noqa: E402

BATCH = 50


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--force", action="store_true", help="re-tier rows that already have one")
    ap.add_argument("--only", nargs="*", default=None, help="restrict to these tickers")
    args = ap.parse_args()

    query: dict = {}
    if not args.force:
        query = {"$or": [{"market_cap_tier": None}, {"market_cap_tier": {"$exists": False}}]}
    if args.only:
        query = {"$and": [query or {}, {"ticker": {"$in": [t.upper() for t in args.only]}}]}

    targets = sorted(
        d["ticker"]
        for d in mongo_store.find_docs("ticker_metadata", query, projection={"ticker": 1})
        if d.get("ticker")
    )
    print(f"targets: {len(targets)} (dry_run={args.dry_run}, force={args.force})")
    if not targets:
        return 0

    import yfinance as yf

    updated, no_cap, errors = 0, [], 0
    for i in range(0, len(targets), BATCH):
        chunk = targets[i : i + BATCH]
        for tkr in chunk:
            cap = None
            try:
                # fast_info avoids the heavyweight .info scrape and rate-limits
                # far less aggressively; fall back to .info when it has no cap.
                fi = yf.Ticker(tkr).fast_info
                cap = getattr(fi, "market_cap", None) or (fi["marketCap"] if "marketCap" in dir(fi) else None)
            except Exception:
                cap = None
            if not cap:
                try:
                    cap = yf.Ticker(tkr).info.get("marketCap")
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    print(f"  {tkr}: lookup failed ({e})")
                    continue
            tier = tier_for_market_cap(cap)
            if not tier:
                no_cap.append(tkr)
                continue
            if args.dry_run:
                print(f"  {tkr}: cap={cap:,} -> {tier} (dry)")
            else:
                mongo_store.update_docs(
                    "ticker_metadata",
                    {"ticker": tkr},
                    {"$set": {"market_cap": cap, "market_cap_tier": tier}},
                )
            updated += 1
        print(f"[{min(i + BATCH, len(targets))}/{len(targets)}] tiered={updated} no_cap={len(no_cap)} errors={errors}")
        time.sleep(1.0)  # stay under yfinance's informal rate limits

    if no_cap:
        # ETFs, trusts and dead symbols have no marketCap — name them so the
        # residue is a visible list, not a silent skip.
        print(f"no market cap from vendor ({len(no_cap)}): {no_cap[:40]}{'...' if len(no_cap) > 40 else ''}")
    print(f"done: tiered={updated}, no_cap={len(no_cap)}, errors={errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
