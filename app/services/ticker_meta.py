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
