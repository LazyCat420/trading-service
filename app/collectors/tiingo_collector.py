"""
tiingo_collector.py — Clean EOD price data from Tiingo.
"""
import logging
import datetime
import httpx
from app.config import settings
from app.db.connection import get_db

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tiingo.com/tiingo/daily"

async def collect_eod_prices(ticker: str, days: int = 30) -> int:
    """Fetch EOD prices for a single ticker."""
    token = settings.TIINGO_API_KEY
    if not token:
        logger.warning("[tiingo] TIINGO_API_KEY not set. Skipping Tiingo EOD collection.")
        return 0

    start_date = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    url = f"{BASE_URL}/{ticker.lower()}/prices"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                url,
                params={"startDate": start_date, "resampleFreq": "daily"},
                headers={"Content-Type": "application/json", "Authorization": f"Token {token}"}
            )
            r.raise_for_status()
            prices_data = r.json()
    except Exception as e:
        logger.error(f"[tiingo] Failed to fetch prices for {ticker}: {e}")
        return 0

    if not isinstance(prices_data, list):
        logger.error(f"[tiingo] Unexpected response layout from Tiingo: {type(prices_data)}")
        return 0

    rows = []
    for day in prices_data:
        date_str = day.get("date", "").split("T")[0]
        o = day.get("open")
        h = day.get("high")
        l = day.get("low")
        c = day.get("close")
        v = day.get("volume", 0)

        if date_str and o is not None and c is not None:
            parsed_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            rows.append((ticker.upper(), parsed_date, float(o), float(h), float(l), float(c), int(v), "tiingo"))

    if rows:
        with get_db() as db:
            db.executemany("""
                INSERT INTO price_history
                (ticker, date, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, date, source) DO UPDATE 
                SET close = EXCLUDED.close, volume = EXCLUDED.volume, open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low
            """, rows)
        logger.info(f"[tiingo] Collected {len(rows)} price history points for {ticker}")
        return len(rows)
    return 0

async def collect_all(tickers: list[str]) -> dict:
    results = {}
    for ticker in tickers:
        count = await collect_eod_prices(ticker, days=30)
        results[ticker] = count
    return results
