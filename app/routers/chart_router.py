"""
Chart Router — serves AI-generated technical analysis chart JSON files.

Endpoint:
    GET /charts/{ticker}.json

This is the endpoint fetched by AgenticChart.jsx in the trading-client:
    fetch(`/tools-api/charts/${ticker}.json`)

The trading-client proxies /tools-api/* to the lazy-tool-service (port 5591),
which then routes it internally. If a chart is requested,
the lazy-tool-service can redirect to it, or the charting tool can write
directly to the shared volume mount.

This router serves charts directly from trading-service locally as a backup,
or if the client wants to hit trading-service directly
  instead of lazy-tool-service for chart data/charts/{TICKER}.json
- This endpoint reads and returns it directly
- The trading-client's tools-api proxy can be updated to hit trading-service
  instead of lazy-tool-service for chart data

For backward compat with the existing /tools-api proxy route hitting 5591,
both services should have this route available.
"""

import asyncio
import json
import logging
import os
import re
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger(__name__)

# Tickers with an analysis currently running (on-demand endpoint below)
_analyses_in_flight: dict[str, float] = {}
_IN_FLIGHT_TTL = 900  # consider a run stale/dead after 15 minutes

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


async def _run_quant_analysis(symbol: str):
    """Run the V3 quant analyst for one ticker so it tool-calls save_trading_chart."""
    try:
        from app.tools.tool_context import set_tool_context
        from app.agents.base_agent import run_agent
        from app.v3.agents import quant_analyst

        cycle_id = f"ondemand-chart-{symbol}-{int(time.time())}"
        set_tool_context(agent_name=quant_analyst.AGENT_NAME, cycle_id=cycle_id)

        user_prompt = (
            f"## Ticker: {symbol}\n"
            f"## Cycle: {cycle_id}\n\n"
            "This is an ON-DEMAND technical analysis requested from the ticker "
            "detail chart. There is no SharedDesk context — fetch what you need "
            "with your tools.\n\n"
            "1. Use your market-data/technical-indicator tools to analyze the ticker.\n"
            "2. Identify support/resistance zones and trendlines.\n"
            "3. Call `save_trading_chart` exactly once with those overlays so the "
            "frontend chart can render them. This step is MANDATORY.\n\n"
            "Then output your final JSON artifact.\n"
        )

        result = await asyncio.wait_for(
            run_agent(
                agent_name=quant_analyst.AGENT_NAME,
                ticker=symbol,
                cycle_id=cycle_id,
                bot_id="ondemand-chart",
                system_prompt=quant_analyst.SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=8192,
                enable_tools=True,
            ),
            timeout=600,
        )
        logger.info(
            "[chart_router] On-demand quant analysis for %s finished (%d loops)",
            symbol, result.get("loops_used", 0),
        )
    except Exception as e:
        logger.error("[chart_router] On-demand quant analysis for %s failed: %s", symbol, e)
    finally:
        _analyses_in_flight.pop(symbol, None)


@router.post("/charts/{ticker}/analyze")
async def trigger_chart_analysis(ticker: str):
    """Kick off an on-demand technical analysis for a ticker.

    Runs the v3_quant_analyst agent in the background; it tool-calls
    save_trading_chart, which writes {TICKER}.json to CHARTS_DIR. The
    frontend's existing polling of GET /charts/{ticker}.json picks it up.
    """
    symbol = ticker.upper().strip()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,10}", symbol):
        raise HTTPException(status_code=400, detail=f"Invalid ticker: {ticker}")

    started = _analyses_in_flight.get(symbol)
    if started and (time.time() - started) < _IN_FLIGHT_TTL:
        return JSONResponse({"status": "already_running", "ticker": symbol})

    _analyses_in_flight[symbol] = time.time()
    asyncio.create_task(_run_quant_analysis(symbol))
    logger.info("[chart_router] On-demand quant analysis started for %s", symbol)
    return JSONResponse({"status": "started", "ticker": symbol})
