"""
Data Rotator — Graceful fallback across multiple financial data providers.

If yfinance is rate-limited, it falls back to FMP, then Polygon, ensuring
the pipeline always gets critical data like OHLCV and Fundamentals.
"""

import logging

from app.collectors import yfinance_collector
from app.collectors import fmp_collector
from app.collectors import finnhub_collector
from app.collectors import polygon_collector
from app.collectors import finviz_scraper
from app.config import settings

logger = logging.getLogger(__name__)


async def fetch_price_history(ticker: str, days_back: int = 365) -> int:
    """Try to fetch price history, falling back across providers until successful."""
    period = "1y" if days_back <= 365 else "5y"

    # 1. Try yfinance
    logger.debug(f"[rotator] Fetching price history for {ticker} via yfinance...")
    try:
        count = await yfinance_collector.collect_price_history(ticker, period=period)
        if count > 0:
            return count
    except Exception as e:
        logger.warning(f"[rotator] yfinance raised error for {ticker} prices: {e}")

    # 2. Fallback to FMP (only if API key is configured)
    if settings.FMP_API_KEY:
        logger.warning(
            f"[rotator] yfinance failed for {ticker} prices. Falling back to FMP..."
        )
        try:
            count = await fmp_collector.collect_price_history(ticker, days_back=days_back)
            if count > 0:
                return count
        except Exception as e:
            logger.warning(f"[rotator] FMP raised error for {ticker} prices: {e}")
    else:
        logger.debug("[rotator] FMP_API_KEY not set, skipping FMP fallback for %s", ticker)

    # 3. Fallback to Polygon (only if API key is configured)
    if settings.POLYGON_API_KEY:
        logger.warning(
            f"[rotator] FMP failed for {ticker} prices. Falling back to Polygon..."
        )
        count = 0
        try:
            count = await polygon_collector.collect_price_history(
                ticker, days_back=days_back
            )
        except Exception as e:
            logger.warning(f"[rotator] Polygon raised error for {ticker} prices: {e}")
    else:
        logger.debug("[rotator] POLYGON_API_KEY not set, skipping Polygon fallback for %s", ticker)
        count = 0

    if count == 0:
        logger.error(
            f"[rotator] ALL providers failed to fetch price history for {ticker}."
        )
    return count


async def fetch_fundamentals(ticker: str) -> bool:
    """Try to fetch fundamentals, falling back across providers."""
    # 1. Try yfinance
    logger.debug(f"[rotator] Fetching fundamentals for {ticker} via yfinance...")
    try:
        success = await yfinance_collector.collect_fundamentals(ticker)
        if success:
            return True
    except Exception as e:
        logger.warning(
            f"[rotator] yfinance raised error for {ticker} fundamentals: {e}"
        )

    # 2. Fallback to FMP (only if API key is configured)
    if settings.FMP_API_KEY:
        logger.warning(
            f"[rotator] yfinance failed for {ticker} fundamentals. Falling back to FMP..."
        )
        try:
            success = await fmp_collector.collect_fundamentals(ticker)
            if success:
                return True
        except Exception as e:
            logger.warning(f"[rotator] FMP raised error for {ticker} fundamentals: {e}")
    else:
        logger.debug("[rotator] FMP_API_KEY not set, skipping FMP fundamentals fallback for %s", ticker)

    # 3. Fallback to Finviz (no API key needed — web scraper)
    logger.warning(
        f"[rotator] FMP failed for {ticker} fundamentals. Falling back to Finviz..."
    )
    success = False
    try:
        success = await finviz_scraper.collect_fundamentals(ticker)
    except Exception as e:
        logger.warning(f"[rotator] Finviz raised error for {ticker} fundamentals: {e}")

    if not success:
        logger.error(
            f"[rotator] ALL providers failed to fetch fundamentals for {ticker}."
        )
    return success


async def fetch_financials(ticker: str) -> int:
    """Try to fetch financials (income statement), falling back across providers."""
    # 1. Try yfinance
    logger.debug(f"[rotator] Fetching financials for {ticker} via yfinance...")
    try:
        count = await yfinance_collector.collect_financials(ticker)
        if count > 0:
            return count
    except Exception as e:
        logger.warning(f"[rotator] yfinance raised error for {ticker} financials: {e}")

    # 2. Fallback to FMP (only if API key is configured)
    count = 0
    if settings.FMP_API_KEY:
        logger.warning(
            f"[rotator] yfinance failed for {ticker} financials. Falling back to FMP..."
        )
        try:
            count = await fmp_collector.collect_financials(ticker)
        except Exception as e:
            logger.warning(f"[rotator] FMP raised error for {ticker} financials: {e}")
    else:
        logger.debug("[rotator] FMP_API_KEY not set, skipping FMP financials fallback for %s", ticker)

    if count == 0:
        logger.info(
            f"[rotator] ALL providers failed to fetch financials for {ticker} (Common for ETFs)."
        )
    return count


async def fetch_balance_sheet(ticker: str) -> int:
    """Try to fetch balance sheet, falling back across providers."""
    # 1. Try yfinance
    logger.debug(f"[rotator] Fetching balance sheet for {ticker} via yfinance...")
    try:
        count = await yfinance_collector.collect_balance_sheet(ticker)
        if count > 0:
            return count
    except Exception as e:
        logger.warning(
            f"[rotator] yfinance raised error for {ticker} balance sheet: {e}"
        )

    # 2. Fallback to FMP (only if API key is configured)
    count = 0
    if settings.FMP_API_KEY:
        logger.warning(
            f"[rotator] yfinance failed for {ticker} balance sheet. Falling back to FMP..."
        )
        try:
            count = await fmp_collector.collect_balance_sheet(ticker)
        except Exception as e:
            logger.warning(f"[rotator] FMP raised error for {ticker} balance sheet: {e}")
    else:
        logger.debug("[rotator] FMP_API_KEY not set, skipping FMP balance sheet fallback for %s", ticker)

    if count == 0:
        logger.info(
            f"[rotator] ALL providers failed to fetch balance sheet for {ticker} (Common for ETFs)."
        )
    return count


async def fetch_analyst_targets(ticker: str) -> bool:
    """Try to fetch analyst price targets, falling back to other providers."""
    # 1. Try Finnhub (uses FINNHUB_API_KEY, checked inside _get_client)
    logger.debug(f"[rotator] Fetching analyst targets for {ticker} via Finnhub...")
    success = False
    try:
        success = await finnhub_collector.collect_analyst_targets(ticker)
        if success:
            return True
    except Exception as e:
        logger.warning(
            f"[rotator] Finnhub raised error for {ticker} analyst targets: {e}"
        )

    # 2. Fallback to FMP (only if API key is configured)
    if settings.FMP_API_KEY:
        logger.warning(
            f"[rotator] Finnhub failed for {ticker} analyst targets. Falling back to FMP..."
        )
        try:
            success = await fmp_collector.collect_analyst_targets(ticker)
            if success:
                return True
        except Exception as e:
            logger.warning(f"[rotator] FMP raised error for {ticker} analyst targets: {e}")
    else:
        logger.debug("[rotator] FMP_API_KEY not set, skipping FMP analyst targets fallback for %s", ticker)

    if not success:
        logger.error(f"[rotator] ALL providers failed to fetch analyst targets for {ticker}.")
    return success


async def collect_all(ticker: str) -> dict:
    """Run all rotational collectors for a given ticker."""
    prices = await fetch_price_history(ticker)
    fundies = await fetch_fundamentals(ticker)
    financials = await fetch_financials(ticker)
    balance = await fetch_balance_sheet(ticker)

    return {
        "ticker": ticker,
        "price_rows": prices,
        "fundamentals": fundies,
        "financial_rows": financials,
        "balance_rows": balance,
    }
