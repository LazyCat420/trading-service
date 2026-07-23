"""Ticker sector / market-cap-tier lookups for diversity bucketing.

Sources: ticker_metadata (sector + market_cap_tier, indexed) with
company_registry.sector as fallback. Fail-open: missing tickers just have no
metadata and are exempt from sector caps.
"""

from __future__ import annotations

import logging

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def get_ticker_meta(tickers: list[str]) -> dict[str, dict]:
    """{ticker: {"sector": str|None, "tier": str|None}} for the given tickers."""
    tickers = [t.upper() for t in tickers if t]
    if not tickers:
        return {}
    meta: dict[str, dict] = {}
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT ticker, sector, market_cap_tier FROM ticker_metadata "
                "WHERE ticker = ANY(%s)",
                [tickers],
            ).fetchall()
            for t, sector, tier in rows:
                meta[t] = {"sector": sector or None, "tier": tier or None}
            missing = [t for t in tickers if t not in meta or not meta[t]["sector"]]
            if missing:
                creg = db.execute(
                    "SELECT symbol, sector FROM company_registry WHERE symbol = ANY(%s)",
                    [missing],
                ).fetchall()
                for sym, sector in creg:
                    entry = meta.setdefault(sym, {"sector": None, "tier": None})
                    entry["sector"] = entry["sector"] or (sector or None)
    except Exception as e:
        logger.warning("[ticker_meta] lookup failed (non-fatal): %s", e)
    return meta
