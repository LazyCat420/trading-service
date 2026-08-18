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

    with get_db() as db:
        # Keep the metadata row usable by the screener: AUM stands in for
        # market cap (drives cap tiers/sorting), name from the fund itself.
        db.execute(
            """
            UPDATE ticker_metadata
            SET name = COALESCE(name, %s),
                market_cap = COALESCE(%s, market_cap),
                updated_at = CURRENT_TIMESTAMP
            WHERE ticker = %s
            """,
            [info.get("shortName") or info.get("longName"),
             info.get("totalAssets"), ticker],
        )
        db.execute(
            """
            INSERT INTO etf_metadata (
                ticker, category, fund_family, total_assets, expense_ratio_pct,
                dividend_yield, ret_3y, ret_5y, nav_price, beta_3y, collected_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (ticker) DO UPDATE SET
                category = COALESCE(EXCLUDED.category, etf_metadata.category),
                fund_family = COALESCE(EXCLUDED.fund_family, etf_metadata.fund_family),
                total_assets = COALESCE(EXCLUDED.total_assets, etf_metadata.total_assets),
                expense_ratio_pct = COALESCE(EXCLUDED.expense_ratio_pct, etf_metadata.expense_ratio_pct),
                dividend_yield = COALESCE(EXCLUDED.dividend_yield, etf_metadata.dividend_yield),
                ret_3y = COALESCE(EXCLUDED.ret_3y, etf_metadata.ret_3y),
                ret_5y = COALESCE(EXCLUDED.ret_5y, etf_metadata.ret_5y),
                nav_price = COALESCE(EXCLUDED.nav_price, etf_metadata.nav_price),
                beta_3y = COALESCE(EXCLUDED.beta_3y, etf_metadata.beta_3y),
                collected_at = CURRENT_TIMESTAMP
            """,
            [
                ticker,
                info.get("category"),
                info.get("fundFamily"),
                info.get("totalAssets"),
                info.get("netExpenseRatio"),
                info.get("yield"),
                info.get("threeYearAverageReturn"),
                info.get("fiveYearAverageReturn"),
                info.get("navPrice"),
                info.get("beta3Year"),
            ],
        )
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
