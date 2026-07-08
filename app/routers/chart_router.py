"""
Chart Router — serves AI-generated technical analysis chart JSON files.

Endpoint:
    GET /charts/{ticker}.json

This is the endpoint fetched by AgenticChart.jsx in the trading-client:
    fetch(`/tools-api/charts/${ticker}.json`)

The trading-client proxies /tools-api/* to the lazy-agent-service (port 5591),
BUT the trading-service also needs to expose its own /charts endpoint so that
the lazy-agent-service can redirect to it, or the charting tool can write
JSON to a shared volume served by both.

The simpler approach used here:
- The trading-service writes chart JSON to /app/data/charts/{TICKER}.json
- This endpoint reads and returns it directly
- The trading-client's tools-api proxy can be updated to hit trading-service
  instead of lazy-agent-service for chart data

For backward compat with the existing /tools-api proxy route hitting 5591,
both services should have this route available.
"""

import json
import logging
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger(__name__)

# Matches the charting_tools.py OUTPUT_DIR logic
_default_charts_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../data/charts")
)
CHARTS_DIR = os.environ.get("CHART_OUTPUT_DIR", _default_charts_dir)


@router.get("/charts/{ticker}.json")
def get_chart_json(ticker: str):
    """Return the AI-generated chart JSON for a ticker, if it exists."""
    symbol = ticker.upper().strip().replace(".json", "")
    json_path = os.path.join(CHARTS_DIR, f"{symbol}.json")

    if not os.path.exists(json_path):
        raise HTTPException(
            status_code=404,
            detail=f"No chart analysis found for {symbol}. Run an analysis first."
        )

    try:
        with open(json_path, "r") as f:
            data = json.load(f)
        return JSONResponse(content=data)
    except Exception as e:
        logger.error("[chart_router] Failed to read chart JSON for %s: %s", symbol, e)
        raise HTTPException(status_code=500, detail=f"Failed to read chart data: {e}")
