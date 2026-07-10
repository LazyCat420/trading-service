import logging
from collections import defaultdict
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.db.connection import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/market-map")
def get_market_map():
    """
    Returns the market map for the S&P 500.
    Returns a hierarchical JSON: Market -> Sector -> Ticker.
    """
    try:
        with get_db() as db:
            # Query S&P 500 tickers, their sector, market cap, and latest price/change
            query = """
            WITH latest_prices AS (
                SELECT ticker, close, open,
                       ROW_NUMBER() OVER(PARTITION BY ticker ORDER BY date DESC) as rn
                FROM price_history
            )
            SELECT tm.ticker, COALESCE(tm.sector, 'Other') as sector, tm.market_cap, lp.close, lp.open
            FROM ticker_metadata tm
            LEFT JOIN latest_prices lp ON tm.ticker = lp.ticker AND lp.rn = 1
            WHERE tm.sp500 = TRUE AND tm.market_cap IS NOT NULL
            """
            
            rows = db.execute(query).fetchall()
            
            sectors = defaultdict(list)
            for row in rows:
                symbol, sector, market_cap, close, open_price = row
                
                # Calculate change %
                change = 0.0
                if close is not None and open_price is not None and open_price > 0:
                    change = (close - open_price) / open_price * 100
                    
                # We need positive values for bubble sizes
                if market_cap and market_cap > 0:
                    sectors[sector].append({
                        "name": symbol,
                        "value": float(market_cap),
                        "change": float(change),
                        "price": float(close) if close is not None else 0.0
                    })
                    
        # Format for D3 pack
        children = []
        for sector, stocks in sectors.items():
            if stocks:
                children.append({
                    "name": sector,
                    "children": stocks
                })
                
        return JSONResponse(content={"name": "Market", "children": children})
    except Exception as e:
        logger.error(f"[market_router] Error fetching market map: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
