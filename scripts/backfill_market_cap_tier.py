"""Backfill ticker_metadata.market_cap_tier for names the mega-cap gate is blind to.

WHY (ch.97, 2026-08-25): the "maximum one mega-cap per cycle" rule tests
`ticker_metadata.market_cap_tier == "mega"`, but the tier is only written by
`load_sp500_universe(enrich=True)` — and boot always runs `enrich=False`, so
510 of 1,049 rows (AAPL and TSLA among them) carried no tier at all. The
enforcement shipped; the data it reads never did.

Two passes, in this order:

  1. FUNDS. Every row whose `asset_class` says ETF/fund is tagged `ETF_TIER`
     through `ensure_ticker_metadata` — the same authority the gatekeeper's
     admission path uses, so the script and the gate cannot disagree. No vendor
     call: the row's own `asset_class` is the evidence. This pass DOES rewrite an
     existing company tier on a fund — measured 2026-09-06, all 47 tiered ETFs
     said "micro" (QQQM at $104B) and 38 more had no tier at all, so the
     mega-cap cap was either blind to them or lying about them.
  2. COMPANIES. Reads market caps from yfinance in batches, maps them through
     the same `tier_for_market_cap` the loader uses, and updates ONLY rows whose
     tier is missing (never overwrites an existing tier — `--force` to re-tier
     everything). Funds are excluded from this pass.

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
from app.services.ticker_meta import (  # noqa: E402
    ETF_ASSET_CLASSES,
    ETF_TIER,
    ensure_ticker_metadata,
)

BATCH = 50


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--force", action="store_true", help="re-tier rows that already have one")
    ap.add_argument("--only", nargs="*", default=None, help="restrict to these tickers")
    args = ap.parse_args()

    # ── Pass 1: funds, from the row's own asset_class ─────────────────────
    fund_query: dict = {"asset_class": {"$in": sorted(ETF_ASSET_CLASSES)}}
    if args.only:
        fund_query["ticker"] = {"$in": [t.upper() for t in args.only]}
    fund_rows = mongo_store.find_docs(
        "ticker_metadata", fund_query,
        projection={"ticker": 1, "market_cap_tier": 1, "market_cap": 1, "_id": 0},
    )
    funds = sorted(d["ticker"] for d in fund_rows if d.get("ticker"))
    to_tag = sorted(d["ticker"] for d in fund_rows if d.get("ticker") and d.get("market_cap_tier") != ETF_TIER)
    print(f"funds: {len(funds)} rows with a fund asset_class; {len(to_tag)} not yet '{ETF_TIER}'")
    if to_tag:
        was = {d["ticker"]: d.get("market_cap_tier") for d in fund_rows}
        for t in to_tag:
            print(f"  {t}: {was.get(t)!r} -> {ETF_TIER}{' (dry)' if args.dry_run else ''}")
        if not args.dry_run:
            tagged = ensure_ticker_metadata(to_tag)
            missed = [t for t in to_tag if tagged.get(t) != ETF_TIER]
            print(f"  tagged {len(to_tag) - len(missed)}/{len(to_tag)}" + (f"; MISSED {missed}" if missed else ""))

    # ── Pass 2: companies, from the vendor ────────────────────────────────
    query: dict = {"ticker": {"$nin": funds}} if funds else {}
    if not args.force:
        query = {"$and": [query or {}, {"$or": [{"market_cap_tier": None}, {"market_cap_tier": {"$exists": False}}]}]}
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
