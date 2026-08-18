"""
ETF Metadata Collector — category, sponsor, AUM, expense ratio, returns.

Source: yfinance .info for tickers with ticker_metadata.asset_class='etf'.
Writes to: etf_metadata (one row per ticker, upserted).

Units (checked against yfinance 1.2.0 on SPY):
- netExpenseRatio is ALREADY a percent (SPY 0.0945 = 0.0945%/yr) — stored as-is.
- yield / threeYearAverageReturn / fiveYearAverageReturn are FRACTIONS.
"""

import asyncio
import datetime
import logging

import yfinance as yf

from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

# Curated majors — the organic universe was ~47 discovery strays with no
# SPY/QQQ/sector SPDRs. Seeded into ticker_metadata (asset_class='etf') on
# every collect_all_etfs run so a fresh DB gets them too.
MAJOR_ETFS = [
    "SPY", "VOO", "IVV", "VTI", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC",
    "SMH", "SOXX", "ARKK", "EEM", "EFA", "VNQ", "SCHD", "JEPI", "VYM", "VUG", "VTV",
    "GLD", "SLV", "TLT", "HYG", "LQD", "BND", "AGG", "USO", "UNG", "BITO", "IBIT",
]


def _seed_major_etfs() -> None:
    """Ensure the curated majors exist in ticker_metadata as ETFs."""
    for t in MAJOR_ETFS:
        mongo_store.update_docs('ticker_metadata', {'ticker': t}, {'$set': {'asset_class': 'etf'}, '$setOnInsert': {'sp500': False}}, upsert=True)


async def collect_etf_metadata(ticker: str) -> bool:
    """Fetch and upsert one ETF's metadata row."""
    try:
        info = await asyncio.to_thread(lambda: yf.Ticker(ticker).info)
    except Exception as e:
        logger.info(f"[etf] {ticker}: info fetch failed: {e}")
        return False
    if not info or info.get("quoteType") not in ("ETF", "MUTUALFUND"):
        logger.info(f"[etf] {ticker}: not an ETF (quoteType={info.get('quoteType') if info else None})")
        return False

    now = datetime.datetime.now(datetime.timezone.utc)

    # Keep the metadata row usable by the screener: AUM stands in for
    # market cap (drives cap tiers/sorting), name from the fund itself.
    # SQL: `SET name = COALESCE(name, %s)` — keep the existing value, fill only
    # when it is NULL. Mongo has no such expression in update_many, so the
    # existing row is read and the COALESCE resolved in Python.
    existing = mongo_query.find_row('ticker_metadata', {'ticker': ticker},
                                    ['name', 'market_cap'])
    new_name = info.get("shortName") or info.get("longName")
    total_assets = info.get("totalAssets")
    meta_set = {'updated_at': now}
    meta_set['name'] = (existing[0] if existing and existing[0] is not None else new_name)
    meta_set['market_cap'] = (total_assets if total_assets is not None
                              else (existing[1] if existing else None))
    mongo_store.update_docs('ticker_metadata', {'ticker': ticker}, {'$set': meta_set})

    # `INSERT ... ON CONFLICT (ticker) DO UPDATE SET c = COALESCE(EXCLUDED.c, etf_metadata.c)`
    # — a new non-NULL value wins, a NULL leaves the stored one alone. Fields
    # whose incoming value is None are simply omitted from the $set, which is
    # exactly that semantic; on an insert they are absent, i.e. NULL.
    incoming = {
        'category': info.get("category"),
        'fund_family': info.get("fundFamily"),
        'total_assets': total_assets,
        'expense_ratio_pct': info.get("netExpenseRatio"),
        'dividend_yield': info.get("yield"),
        'ret_3y': info.get("threeYearAverageReturn"),
        'ret_5y': info.get("fiveYearAverageReturn"),
        'nav_price': info.get("navPrice"),
        'beta_3y': info.get("beta3Year"),
    }
    doc = {'ticker': ticker, 'collected_at': now}
    doc.update({k: v for k, v in incoming.items() if v is not None})
    mongo_store.upsert_doc('etf_metadata', {'ticker': ticker}, doc)

    logger.info(f"[etf] {ticker}: metadata written ({info.get('category')})")
    return True


async def collect_all_etfs() -> dict:
    """Refresh every ETF in ticker_metadata. Small set — runs in minutes."""
    try:
        _seed_major_etfs()
    except Exception as e:
        logger.warning(f"[etf] major-ETF seed failed: {e}")
    rows = mongo_query.find_rows('ticker_metadata', {'asset_class': 'etf'}, ['ticker'])
    tickers = [r[0] for r in rows]
    done = 0
    for t in tickers:
        try:
            if await collect_etf_metadata(t):
                done += 1
        except Exception as e:
            logger.warning(f"[etf] {t} failed: {e}")
    logger.info(f"[etf] refreshed {done}/{len(tickers)} ETFs")
    return {"total": len(tickers), "done": done}
