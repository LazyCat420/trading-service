"""
Technical Analyst Agent — Generates strict JSON technical overlays for charting.

Runs during the pipeline (e.g. data completeness pre-generation or phase 4) to 
analyze recent price history and dictate support/resistance lines, trendlines, 
and moving averages. The result is pushed to `lazy-tool-service` for rendering.
"""

import logging
import json
import httpx
import os
from app.agents.base_agent import run_agent
from app.db.connection import get_db
from app.config.guardrails import ANTI_HALLUCINATION_BLOCK

logger = logging.getLogger(__name__)

TECHNICAL_ANALYST_SYSTEM_PROMPT = """You are an elite quantitative technical analyst.
Your job is to analyze the provided OHLCV data and generate a strict JSON payload that defines technical overlays.
You must identify:
1. Two support zones (min/max price ranges).
2. Two resistance zones (min/max price ranges).
3. One overarching trendline (start date/price and end date/price).

Your response MUST be a single raw JSON object matching this structure exactly (no markdown formatting, no backticks, no text outside the JSON, no 'memory' objects or preamble):
{
  "overlays": [
    {
      "type": "support",
      "y0": 145.5,
      "y1": 147.0,
      "color": "green",
      "reasoning": "Historical bounce level on high volume"
    },
    {
      "type": "resistance",
      "y0": 155.0,
      "y1": 157.5,
      "color": "red",
      "reasoning": "Recent swing high rejection"
    },
    {
      "type": "trendline",
      "x0": "2024-01-01",
      "y0": 140.0,
      "x1": "2024-03-01",
      "y1": 150.0,
      "color": "blue",
      "reasoning": "Ascending support line connecting recent higher lows"
    }
  ]
}
""" + ANTI_HALLUCINATION_BLOCK

def _fetch_recent_ohlcv(ticker: str, limit: int = 60) -> str:
    """Fetch recent OHLCV data from the database and format it as a table."""
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT date, open, high, low, close, volume FROM price_history "
                "WHERE ticker = %s ORDER BY date DESC LIMIT %s",
                [ticker, limit]
            ).fetchall()
            if not rows:
                return "No price history available."
            
            # Reverse to chronological order
            rows = rows[::-1]
            data_str = "Date | Open | High | Low | Close | Volume\n"
            for r in rows:
                ds = r[0].strftime('%Y-%m-%d') if hasattr(r[0], 'strftime') else str(r[0])
                data_str += f"{ds} | {r[1]:.2f} | {r[2]:.2f} | {r[3]:.2f} | {r[4]:.2f} | {r[5]}\n"
            return data_str
    except Exception as e:
        logger.error(f"Failed to fetch OHLCV for {ticker}: {e}")
        return "Failed to fetch price history."

async def run_technical_analyst(ticker: str, cycle_id: str = "JIT", bot_id: str = "system") -> bool:
    """
    Harness function: 
    1. Fetches data.
    2. Runs the agent to get JSON.
    3. Posts the JSON to lazy-tool-service.
    """
    logger.info("[TECHNICAL_ANALYST] Starting charting analysis for %s", ticker)
    
    # 1. Fetch Data
    ohlcv_context = _fetch_recent_ohlcv(ticker)
    
    user_prompt = (
        f"Analyze the following recent daily OHLCV data for {ticker}:\n\n"
        f"{ohlcv_context}\n\n"
        "Output ONLY the strict JSON overlay specification."
    )
    
    # 2. Run Agent
    try:
        result = await run_agent(
            agent_name="technical_analyst",
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            system_prompt=TECHNICAL_ANALYST_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=2000,
            enable_tools=False
        )
    except Exception as e:
        logger.error("[TECHNICAL_ANALYST] Agent execution failed: %s", e)
        return False
        
    content = result.get("response", "").strip()
    
    from app.utils.text_utils import parse_json_response
    try:
        overlays = parse_json_response(content)
        if not overlays or "overlays" not in overlays:
            raise ValueError("Parsed JSON is empty or missing 'overlays' key")
    except Exception as e:
        logger.error("[TECHNICAL_ANALYST] Failed to parse JSON from agent: %s | Output: %s", e, content)
        return False
        
    # 3. Post to Lazy-Tool-Service
    LAZY_TOOL_URL = os.getenv("LAZY_TOOL_SERVICE_URL", "http://10.0.0.16:5591")
    endpoint = f"{LAZY_TOOL_URL}/execute/save_trading_chart"
    
    payload = {
        "ticker": ticker,
        "overlays": overlays.get("overlays", []),
        "period": "3mo" # pass period for the tool to re-fetch identical bounds if needed
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(endpoint, json=payload, timeout=30.0)
            resp.raise_for_status()
            logger.info("[TECHNICAL_ANALYST] Successfully saved trading chart overlays for %s via %s", ticker, endpoint)
            return True
    except Exception as e:
        logger.error("[TECHNICAL_ANALYST] Failed to POST to lazy-tool-service: %s", e)
        return False
