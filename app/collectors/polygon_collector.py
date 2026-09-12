"""
Polygon.io Collector — Fallback for OHLCV prices.

Pure data collector. No LLM calls.
Writes to: price_history
Requires: POLYGON_API_KEY in .env (free tier = 5 calls/min)
"""

import logging
import datetime
from app.config import settings
from app.services.request_utils import SmartClient
from app.db import mongo_store
from app.collectors import price_window

logger = logging.getLogger(__name__)


def _get_key() -> str:
    """Polygon credential, under either of the two names we store it as.

    The news rotator has always read `POLYGON_API_KEY or MASSIVE_API_KEY`
    (news_api_rotator.build_providers_from_settings), but the price path read
    only the first. On the live container POLYGON_API_KEY is EMPTY and
    MASSIVE_API_KEY is set — so Polygon served news happily while the Polygon
    PRICE fallback was unreachable for every ticker, which is precisely the
    fallback needed when yfinance withholds the latest session.
    """
    key = settings.POLYGON_API_KEY or settings.MASSIVE_API_KEY
    if not key:
        raise ValueError("Neither POLYGON_API_KEY nor MASSIVE_API_KEY set in .env")
    return key


async def collect_price_history(ticker: str, days_back: int = 365) -> int:
    """Fetch OHLCV history and upsert into price_history table."""
    try:
        api_key = _get_key()
    except ValueError:
        return 0

    to_date = datetime.date.today()
    # Ask only for the bars we are missing. Polygon is the LAST fallback: it is
    # called precisely because yfinance and FMP left the newest session
    # missing, so requesting a full year to obtain one day was the worst ratio
    # in the chain.
    days_back = price_window.incremental_days_back(ticker, "polygon", days_back)
    from_date = to_date - datetime.timedelta(days=days_back)

    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"

    async with SmartClient(
        base_delay=12.0, max_retries=3
    ) as client:  # 5 calls/min = 12s delay ideally
        resp = await client.get(
            url,
            params={
                "apiKey": api_key,
                "adjusted": "true",
                "sort": "desc",
                "limit": 50000,
            },
        )
        if resp.status_code != 200:
            logger.info(
                f"[polygon] Error fetching price history for {ticker}: HTTP {resp.status_code}"
            )
            return 0
        data = resp.json()

    results = data.get("results", [])
    if not results:
        logger.info(f"[polygon] No price data for {ticker}")
        return 0

    # ONE bulk_write instead of a round-trip per bar. Measured 2026-09-12
    # against the live store, 252 bars: 4.40 s per-row vs 0.069 s bulk (63x).
    docs = []
    for day in results:
        try:
            # Polygon timestamps are in milliseconds
            date_obj = datetime.datetime.fromtimestamp(
                day["t"] / 1000.0, tz=datetime.UTC
            ).date()

            docs.append({'ticker': ticker, 'date': date_obj,
                         'open': float(day.get("o", 0)), 'high': float(day.get("h", 0)),
                         'low': float(day.get("l", 0)), 'close': float(day.get("c", 0)),
                         'volume': int(day.get("v", 0)), 'source': 'polygon'})
        except Exception:
            continue

    mongo_store.bulk_upsert(
        'price_history', docs, key_field=("ticker", "date", "source"), insert_only=True
    )
    # Count the bars we submitted, exactly as the per-row loop did. Callers
    # read 0 as "total outage", so this must not become the store's idea of
    # how many rows were NEW — insert_only means a current series writes none.
    count = len(docs)

    logger.info(f"[polygon] {ticker}: {count} price rows written")
    return count


async def collect_all(ticker: str) -> dict:
    """Run all Polygon collectors."""
    prices = await collect_price_history(ticker)
    return {"ticker": ticker, "price_rows": prices, "source": "polygon"}
