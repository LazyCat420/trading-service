import logging
from collections import defaultdict
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.db.connection import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/market-map")
def get_market_map(days: int = 7):
    """
    Returns the market map timeline for the S&P 500 over the last N days.
    """
    try:
        with get_db() as db:
            dates_query = "SELECT DISTINCT date FROM price_history ORDER BY date DESC LIMIT %s"
            dates_res = db.execute(dates_query, (days,)).fetchall()
            if not dates_res:
                return JSONResponse({"dates": [], "data": {}})
            
            dates = [row[0] for row in dates_res]
            dates.sort() # Oldest to newest
            
            min_date = dates[0]
            max_date = dates[-1]
            
            query = """
            SELECT tm.ticker, COALESCE(tm.sector, 'Other') as sector, tm.market_cap, ph.date, ph.close, ph.open,
                   ph.volume, tm.name, tm.industry, tm.market_cap_tier
            FROM ticker_metadata tm
            JOIN price_history ph ON tm.ticker = ph.ticker
            WHERE tm.sp500 = TRUE AND tm.market_cap IS NOT NULL
              AND ph.date >= %s AND ph.date <= %s
            """

            rows = db.execute(query, (min_date, max_date)).fetchall()

            dates_str = [d.isoformat() for d in dates]
            data_map = defaultdict(list)
            # Per-ticker facts that don't change day to day — sent once instead
            # of being repeated across every date entry (503 tickers × N days).
            meta = {}

            for row in rows:
                ticker, sector, market_cap, date, close, open_price, volume, company, industry, tier = row
                date_str = date.isoformat()

                change = 0.0
                if close is not None and open_price is not None and open_price > 0:
                    change = (close - open_price) / open_price * 100

                if market_cap and market_cap > 0:
                    data_map[date_str].append({
                        "name": ticker,
                        "sector": sector,
                        "value": float(market_cap),
                        "change": change,
                        "price": float(close) if close else 0,
                        "volume": int(volume) if volume else 0,
                    })
                    if ticker not in meta:
                        meta[ticker] = {
                            "company": company or ticker,
                            "industry": industry or "",
                            "tier": tier or "",
                        }

            return JSONResponse({
                "dates": dates_str,
                "data": data_map,
                "meta": meta
            })
    except Exception as e:
        logger.error(f"Error fetching market map: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
