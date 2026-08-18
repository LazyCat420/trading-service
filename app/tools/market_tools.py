"""Market Tools — Agent tools for broad market trends and sector performance.

Pure MongoDB implementation.
"""

import logging
from app.tools.registry import registry, PermissionLevel
from app.db import mongo_store

logger = logging.getLogger(__name__)


@registry.register(
    name="get_market_map_data",
    description="Fetches the top movers and market map data for S&P 500 components, grouped by sector. Use this to analyze broad market trends and sector performance for the day.",
    permission=PermissionLevel.READ_ONLY
)
async def get_market_map_data(
    top_n_per_sector: int = 5,
) -> dict:
    """
    Returns the market map for the S&P 500, summarizing the top gainers and losers per sector.
    """
    try:
        sp500_meta = mongo_store.find_docs(
            "ticker_metadata",
            {"sp500": True, "market_cap": {"$ne": None}}
        )

        sectors: dict[str, list[dict]] = {}
        for tm in sp500_meta:
            ticker = tm.get("ticker")
            if not ticker:
                continue

            sector = tm.get("sector") or "Other"
            market_cap = tm.get("market_cap", 0)

            latest_prices = mongo_store.find_docs(
                "price_history",
                {"ticker": ticker},
                sort=[("date", -1)],
                limit=1,
            )

            close = None
            open_price = None
            if latest_prices:
                close = latest_prices[0].get("close")
                open_price = latest_prices[0].get("open")

            change = 0.0
            if close is not None and open_price is not None and open_price > 0:
                change = (close - open_price) / open_price * 100

            if sector not in sectors:
                sectors[sector] = []

            sectors[sector].append({
                "ticker": ticker,
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
