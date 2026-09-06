"""Ticker sector / market-cap-tier lookups for diversity bucketing.

Sources: ticker_metadata (sector + market_cap_tier) with
company_registry.sector as fallback in pure MongoDB.
"""

from __future__ import annotations

import logging
from app.db import mongo_store

logger = logging.getLogger(__name__)

# An ETF is not a company, so it does not get a company's size bucket. Its own
# `ticker_metadata.asset_class` is the authority — the row has always carried
# it. Kept deliberately separate from `tier_for_market_cap`: the company
# buckets and this label have different authorities and must not drift into
# each other, so no arithmetic on a market cap can ever produce ETF_TIER.
ETF_TIER = "etf"

# `asset_class` values that mean "a fund, not an operating company".
ETF_ASSET_CLASSES = frozenset({"etf", "etn", "fund", "mutualfund"})


def get_ticker_meta(tickers: list[str]) -> dict[str, dict]:
    """{ticker: {"sector": str|None, "tier": str|None}} for the given tickers from MongoDB."""
    tickers = [t.upper() for t in tickers if t]
    if not tickers:
        return {}
    meta: dict[str, dict] = {}
    try:
        docs = mongo_store.find_docs(
            "ticker_metadata",
            {"ticker": {"$in": tickers}},
            projection={"ticker": 1, "sector": 1, "market_cap_tier": 1, "_id": 0}
        )
        for d in docs:
            t = d.get("ticker")
            if t:
                meta[t] = {
                    "sector": d.get("sector") or None,
                    "tier": d.get("market_cap_tier") or None,
                }
        missing = [t for t in tickers if t not in meta or not meta[t]["sector"]]
        if missing:
            creg_docs = mongo_store.find_docs(
                "company_registry",
                {"symbol": {"$in": missing}},
                projection={"symbol": 1, "sector": 1, "_id": 0}
            )
            for d in creg_docs:
                sym = d.get("symbol")
                if sym:
                    entry = meta.setdefault(sym, {"sector": None, "tier": None})
                    entry["sector"] = entry["sector"] or (d.get("sector") or None)
    except Exception as e:
        logger.warning("[ticker_meta] lookup failed (non-fatal): %s", e)
    return meta


def ensure_ticker_metadata(tickers: list[str] | None) -> dict[str, str]:
    """Give an untagged ticker a `market_cap_tier` before the caps read it.

    Returns {ticker: tier} for every name that ends up tiered. A COMPANY's
    existing tier always wins — a stale vendor read must never beat what is on
    file. An ETF is the one correction: see below.

    WHY THIS EXISTS (measured 2026-09-06). `cycle-v3-1788646388` selected ZS,
    analysed it for 100 minutes and BOUGHT it (3.0808 @ $169.84) with zero rows
    in `ticker_metadata`; SE, sold the same day, had none either. No row means
    no `sector` and no `market_cap_tier`, and both admission caps read exactly
    those fields:

      * the sector cap skips a name whose sector is falsy, so an unknown sector
        is EXEMPT rather than capped;
      * the mega-cap cap tests `market_cap_tier == "mega"`, so an untiered name
        can never be the mega-cap it might actually be.

    WHY IT READS THE ROW FIRST (measured 2026-09-06, `cycle-v3-1788660665`).
    The first version of this function asked Mongo for `market_cap_tier` and
    nothing else, so it could not see the two fields that answer the question.
    It called a price vendor for SCHD, got nothing an ETF reports as
    `marketCap`, and logged "no market cap on file" about a document holding
    `market_cap: 112_337_240_064` and `asset_class: "etf"`. Of 39 untiered rows
    38 were ETFs in exactly that state; the 39th, BK, is the only row in the
    collection where that sentence was ever true.

    THE ETF CORRECTION. All 47 ETFs that did carry a tier said "micro" — QQQM
    at $104B, JEPI at $46B — a number `tier_for_market_cap` cannot return for
    those caps, so the label disagreed with its own document. A fund is tagged
    ETF_TIER from its `asset_class` even when a company tier is already there,
    because that is a category correction from the row itself, not a vendor
    overruling stored data. Nothing else is ever overwritten.

    FAILS OPEN. A vendor error, no cap anywhere, or an absent yfinance leaves
    the ticker exactly as it is today: untiered, and admitted. Refusing to
    analyse a name because a price vendor was slow is worse than analysing it
    uncapped.
    """
    names = [str(t).upper().strip() for t in (tickers or []) if str(t or "").strip()]
    if not names:
        return {}

    try:
        existing = mongo_store.find_docs(
            "ticker_metadata",
            {"ticker": {"$in": names}},
            # Every field the decision needs. Asking for fewer is the defect
            # this function was rewritten to fix.
            projection={
                "ticker": 1, "market_cap_tier": 1, "market_cap": 1,
                "asset_class": 1, "_id": 0,
            },
        ) or []
    except Exception as e:  # noqa: BLE001 — never raise into the gatekeeper
        logger.warning("[TickerMeta] could not read metadata for %s: %s", names, e)
        return {}

    rows = {
        d.get("ticker"): d
        for d in existing
        if isinstance(d, dict) and d.get("ticker")
    }

    def _persist(tkr: str, fields: dict) -> bool:
        try:
            mongo_store.update_docs(
                "ticker_metadata", {"ticker": tkr},
                {"$set": fields, "$setOnInsert": {"sp500": False}},
                upsert=True,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("[TickerMeta] %s: could not persist tier: %s", tkr, e)
            return False

    out: dict[str, str] = {}
    unresolved: list[str] = []
    funds: list[str] = []

    for tkr in names:
        row = rows.get(tkr) or {}
        tier = row.get("market_cap_tier")
        asset_class = str(row.get("asset_class") or "").strip().lower()

        if asset_class in ETF_ASSET_CLASSES:
            if tier == ETF_TIER:
                out[tkr] = ETF_TIER
            elif _persist(tkr, {"market_cap_tier": ETF_TIER}):
                out[tkr] = ETF_TIER
                funds.append(tkr)
            continue

        if tier:
            out[tkr] = tier  # a company's stored tier always wins
            continue

        unresolved.append(tkr)

    if funds:
        logger.info(
            "[TickerMeta] tagged %d fund(s) %r from asset_class, so the "
            "mega-cap cap counts operating companies only: %s",
            len(funds), ETF_TIER, funds,
        )
    if not unresolved:
        return out

    try:
        import yfinance as yf

        if yf is None:
            raise ImportError("yfinance is unavailable")
        vendor_available = True
    except Exception as e:  # noqa: BLE001
        logger.warning("[TickerMeta] no price vendor to tier %s: %s", unresolved, e)
        vendor_available = False

    from app.data.sp500_universe import tier_for_market_cap

    filled: list[str] = []
    for tkr in unresolved:
        cap, from_vendor = None, False
        if vendor_available:
            try:
                # fast_info first: it avoids the heavyweight .info scrape and
                # rate-limits far less aggressively (same order the backfill
                # script uses, so the two cannot disagree about the source).
                fast = yf.Ticker(tkr).fast_info
                if fast is not None:
                    cap = getattr(fast, "market_cap", None)
                    if cap is None:
                        try:
                            cap = fast["marketCap"]
                        except Exception:  # noqa: BLE001
                            cap = None
                if not cap:
                    cap = (yf.Ticker(tkr).info or {}).get("marketCap")
                from_vendor = bool(cap)
            except Exception as e:  # noqa: BLE001 — fail open, per ticker
                logger.warning("[TickerMeta] %s: market-cap lookup failed: %s", tkr, e)

        if not cap:
            # The vendor had nothing. The row may still hold a usable cap —
            # this is the branch whose absence produced a log line asserting
            # "no market cap on file" about a document that had one.
            cap = (rows.get(tkr) or {}).get("market_cap")

        tier = tier_for_market_cap(cap)
        if not tier:
            logger.info(
                "[TickerMeta] %s: no market cap from the vendor or on the row "
                "— left untiered", tkr,
            )
            continue

        fields = {"market_cap_tier": tier}
        if from_vendor:
            fields["market_cap"] = cap
        if not _persist(tkr, fields):
            continue

        out[tkr] = tier
        filled.append(tkr)

    if filled:
        logger.info(
            "[TickerMeta] backfilled market_cap_tier for %d ticker(s) the "
            "admission caps could not see: %s", len(filled), filled,
        )
    return out
