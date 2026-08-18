import logging
from collections import defaultdict
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.db import mongo_store, mongo_query

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/market-map")
def get_market_map(days: int = 7):
    """
    Returns the market map timeline for the S&P 500 over the last N days.
    """
    try:
        # Get distinct dates from price_history
        pipeline = [
            {"$group": {"_id": "$date"}},
            {"$sort": {"_id": -1}},
            {"$limit": days},
        ]
        date_docs = mongo_store.aggregate("price_history", pipeline)
        if not date_docs:
            return JSONResponse({"dates": [], "data": {}, "meta": {}})

        dates = [d["_id"] for d in date_docs if d.get("_id")]
        dates.sort()  # Oldest to newest
        if not dates:
            return JSONResponse({"dates": [], "data": {}, "meta": {}})

        min_date = dates[0]
        max_date = dates[-1]

        # Query ticker metadata for S&P 500 tickers
        meta_docs = mongo_store.find_docs("ticker_metadata", {"sp500": True, "market_cap": {"$ne": None}})
        meta_by_ticker = {d["ticker"]: d for d in meta_docs if d.get("ticker")}

        if not meta_by_ticker:
            return JSONResponse({"dates": [], "data": {}, "meta": {}})

        sp500_tickers = list(meta_by_ticker.keys())

        # Query price history for these tickers in the date range
        prices = mongo_store.find_docs("price_history", {
            "ticker": {"$in": sp500_tickers},
            "date": {"$gte": min_date, "$lte": max_date},
        })

        dates_str = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in dates]
        data_map = defaultdict(list)
        meta = {}

        for p in prices:
            ticker = p.get("ticker")
            t_meta = meta_by_ticker.get(ticker, {})
            sector = t_meta.get("sector") or "Other"
            market_cap = t_meta.get("market_cap")
            date = p.get("date")
            date_str = date.isoformat() if hasattr(date, "isoformat") else str(date)
            close = p.get("close")
            open_price = p.get("open")
            volume = p.get("volume")

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
                        "company": t_meta.get("name") or ticker,
                        "industry": t_meta.get("industry") or "",
                        "tier": t_meta.get("market_cap_tier") or "",
                    }

        return JSONResponse({
            "dates": dates_str,
            "data": data_map,
            "meta": meta
        })
    except Exception as e:
        logger.error(f"Error fetching market map: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
