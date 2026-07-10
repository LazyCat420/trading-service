import logging
from pydantic import Field
from app.tools.registry import registry, PermissionLevel
from app.db.connection import get_db

logger = logging.getLogger(__name__)

@registry.register(
    name="get_market_map_data",
    description="Fetches the top movers and market map data for S&P 500 components, grouped by sector. Use this to analyze broad market trends and sector performance for the day.",
    permission=PermissionLevel.READ_ONLY
)
async def get_market_map_data(
    top_n_per_sector: int = Field(default=5, description="Number of top gainers/losers to return per sector to avoid overwhelming the context.")
) -> dict:
    """
    Returns the market map for the S&P 500, summarizing the top gainers and losers per sector.
    """
    try:
        with get_db() as db:
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
            
            # Organize by sector
            sectors = {}
            for row in rows:
                symbol, sector, market_cap, close, open_price = row
                
                change = 0.0
                if close is not None and open_price is not None and open_price > 0:
                    change = (close - open_price) / open_price * 100
                        
                if sector not in sectors:
                    sectors[sector] = []
                    
                sectors[sector].append({
                    "ticker": symbol,
                    "change": change,
                    "market_cap": float(market_cap) if market_cap else 0,
                    "price": float(close) if close else 0
                })
                
            summary = {}
            top_n = int(top_n_per_sector)
            for sector, stocks in sectors.items():
                stocks_sorted = sorted(stocks, key=lambda x: x["change"], reverse=True)
                top_gainers = stocks_sorted[:top_n]
                top_losers = stocks_sorted[-top_n:] if len(stocks_sorted) > top_n else []
                
                summary[sector] = {
                    "top_gainers": top_gainers,
                    "top_losers": top_losers,
                    "total_tracked": len(stocks)
                }
                
            return {"status": "success", "data": summary}
    except Exception as e:
        logger.error(f"Error in get_market_map_data tool: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
