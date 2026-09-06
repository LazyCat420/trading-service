"""Ticker sector / market-cap-tier lookups for diversity bucketing.

Sources: ticker_metadata (sector + market_cap_tier) with
company_registry.sector as fallback in pure MongoDB.
"""

from __future__ import annotations

import logging
from app.db import mongo_store

logger = logging.getLogger(__name__)


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

    Returns {ticker: tier} for the names it filled in. Only touches rows whose
    tier is MISSING — an existing value always wins, because a stale vendor
    read must never beat what is already on file.

    WHY THIS EXISTS (measured 2026-09-06). `cycle-v3-1788646388` selected ZS,
    analysed it for 100 minutes and BOUGHT it (3.0808 @ $169.84). ZS has zero
    rows in `ticker_metadata` — 1,049 other tickers have one — and so does SE,
    which the bot SOLD the same day. No row means no `sector` and no
    `market_cap_tier`, and both admission caps read exactly those fields:

      * the sector cap skips a name whose sector is falsy, so an unknown sector
        is EXEMPT rather than capped;
      * the mega-cap cap tests `market_cap_tier == "mega"`, so an untiered name
        can never be the mega-cap it might actually be.

    The `GATEKEEPER_SELECTED` event was already reporting it (`tier_unknown:
    ['ZS']`) and nothing acted on the report. Same shape as the 2026-08-25
    finding behind `scripts/backfill_market_cap_tier.py` — "the enforcement
    shipped; the data it reads never did", 510 of 1,049 rows untiered — except
    that script is manual.

    FAILS OPEN. A vendor error, a missing marketCap (ETFs, trusts, dead
    symbols) or an absent yfinance all leave the ticker exactly as it is today:
    tier_unknown, and admitted. Refusing to analyse a name because a price
    vendor was slow is worse than analysing it uncapped.
    """
    names = [str(t).upper().strip() for t in (tickers or []) if str(t or "").strip()]
    if not names:
        return {}

    try:
        existing = mongo_store.find_docs(
            "ticker_metadata",
            {"ticker": {"$in": names}},
            projection={"ticker": 1, "market_cap_tier": 1, "_id": 0},
        ) or []
    except Exception as e:  # noqa: BLE001 — never raise into the gatekeeper
        logger.warning("[TickerMeta] could not read metadata for %s: %s", names, e)
        return {}

    tiered = {
        d.get("ticker"): d.get("market_cap_tier")
        for d in existing
        if isinstance(d, dict) and d.get("market_cap_tier")
    }
    missing = [t for t in names if not tiered.get(t)]
    out = {t: v for t, v in tiered.items() if t in names and v}
    if not missing:
        return out

    try:
        import yfinance as yf

        if yf is None:
            raise ImportError("yfinance is unavailable")
    except Exception as e:  # noqa: BLE001
        logger.warning("[TickerMeta] no price vendor to tier %s: %s", missing, e)
        return out

    from app.data.sp500_universe import tier_for_market_cap

    filled: list[str] = []
    for tkr in missing:
        cap = None
        try:
            # fast_info first: it avoids the heavyweight .info scrape and
            # rate-limits far less aggressively (same order the backfill
            # script uses, so the two cannot disagree about the source).
            fast = yf.Ticker(tkr).fast_info
            cap = None
            if fast is not None:
                cap = getattr(fast, "market_cap", None)
                if cap is None:
                    try:
                        cap = fast["marketCap"]
                    except Exception:  # noqa: BLE001
                        cap = None
            if not cap:
                cap = (yf.Ticker(tkr).info or {}).get("marketCap")
        except Exception as e:  # noqa: BLE001 — fail open, per ticker
            logger.warning("[TickerMeta] %s: market-cap lookup failed: %s", tkr, e)
            continue

        tier = tier_for_market_cap(cap)
        if not tier:
            # No marketCap at all — an ETF, a trust or a dead symbol. Writing a
            # tier here would be inventing one.
            logger.info("[TickerMeta] %s: no market cap on file — left untiered", tkr)
            continue

        try:
            mongo_store.update_docs(
                "ticker_metadata",
                {"ticker": tkr},
                {"$set": {"market_cap": cap, "market_cap_tier": tier},
                 "$setOnInsert": {"sp500": False}},
                upsert=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[TickerMeta] %s: could not persist tier: %s", tkr, e)
            continue

        out[tkr] = tier
        filled.append(tkr)

    if filled:
        logger.info(
            "[TickerMeta] backfilled market_cap_tier for %d ticker(s) the "
            "admission caps could not see: %s", len(filled), filled,
        )
    return out
