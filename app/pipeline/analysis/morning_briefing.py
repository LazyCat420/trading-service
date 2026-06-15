"""
Morning Briefing — Compare and contrast recent stock reports at the start of the trading day.
"""

import json
import logging
from datetime import datetime, timezone

from app.config import settings
from app.db.connection import get_db
from app.trading.portfolio import get_current_state
from app.trading.watchlist import get_active
from app.pipeline.analysis.thesis_store import get_thesis
from app.services.vllm_client import llm, Priority
from app.services.prism_agent_caller import call_prism_agent

logger = logging.getLogger(__name__)

async def generate_morning_briefing() -> str:
    """Generate a morning briefing comparing recent stock analyses.

    Steps:
      1. Check feature toggle
      2. Gather portfolio + watchlist tickers
      3. Extract recent theses (within 5 days)
      4. Verify LLM reachability
      5. Send to LLM via Prism for cross-analysis
      6. Persist to morning_briefings table

    Returns:
        The generated briefing markdown string.
        On failure, returns a short fallback message.
    """
    if not getattr(settings, "MORNING_BRIEFING_ENABLED", True):
        logger.info("[MORNING BRIEFING] Disabled via config")
        return "Morning briefing is disabled."

    logger.info("[MORNING BRIEFING] Generating morning briefing...")

    # 1. Gather target universe (Portfolio + Watchlist)
    state = get_current_state()
    portfolio_tickers = [p["ticker"] for p in state.get("positions", [])]

    watchlist_tickers = [w["ticker"] for w in get_active()]

    target_tickers = list(set(portfolio_tickers + watchlist_tickers))
    logger.info(
        "[MORNING BRIEFING] Target universe: %d tickers", len(target_tickers)
    )

    # 2. Extract recent theses
    context_parts = []
    evaluated_tickers = []

    for ticker in target_tickers:
        thesis = get_thesis(ticker)
        if thesis:
            # Check if thesis is relatively recent (e.g., within the last 5 days)
            age_days = (
                (datetime.now(timezone.utc) - thesis.updated_at).total_seconds()
                / (3600 * 24)
            )
            if age_days <= 5:
                context_parts.append(
                    f"## {ticker}\n"
                    f"Verdict: {thesis.verdict}\n"
                    f"Confidence: {thesis.confidence}%\n"
                    f"Updated At: {thesis.updated_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                    f"Summary: {thesis.summary}\n"
                )
                evaluated_tickers.append(ticker)

    if not context_parts:
        logger.warning(
            "[MORNING BRIEFING] No recent theses found for the target universe."
        )
        return "No recent data available to generate a morning briefing."

    context = "\n".join(context_parts)

    # System prompt is now loaded dynamically by Prism from app/agents/custom/morning_briefing.py

    # 3. Check LLM reachability
    jetson_ok = await llm.health()
    if not jetson_ok:
        logger.warning(
            "[MORNING BRIEFING] Jetson unreachable — skipping briefing generation"
        )
        return "Morning briefing skipped: LLM endpoint unreachable."

    logger.info(
        "[MORNING BRIEFING] Running LLM analysis on %d theses...",
        len(evaluated_tickers),
    )

    # 4. Run LLM (with 1 retry on failure)
    max_retries = 2
    response = ""
    tokens = 0
    ms = 0

    for attempt in range(max_retries):
        try:
            response, tokens, ms = await call_prism_agent(
                agent_id="CUSTOM_MORNING_BRIEFING_AGENT",
                user_message=context,
                fallback_system_prompt="",
                fallback_agent_name="morning_briefing_analyst",
                temperature=0.3,
                max_tokens=1500,
                priority=Priority.HIGH,
            )
            if response:
                logger.info(
                    "[MORNING BRIEFING] LLM completed (%d tokens, %dms)",
                    tokens,
                    ms,
                )
                break
        except Exception as e:
            logger.warning(
                "[MORNING BRIEFING] LLM call failed (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                e,
            )
            if attempt == max_retries - 1:
                logger.error(
                    "[MORNING BRIEFING] All LLM attempts failed. Returning fallback."
                )
                return (
                    "Morning briefing generation failed after retries. "
                    f"Tickers evaluated: {', '.join(evaluated_tickers)}"
                )

    # 5. Save to DB
    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO morning_briefings (report_content, tickers_evaluated)
                VALUES (%s, %s)
                """,
                [response, evaluated_tickers],
            )
        logger.info("[MORNING BRIEFING] Saved to database successfully.")
    except Exception as e:
        logger.error("[MORNING BRIEFING] Failed to save to DB: %s", e)

    return response

from app.utils.tz import utc_iso

def get_latest_morning_briefing() -> dict | None:
    """Fetch the most recent morning briefing from the database."""
    try:
        with get_db() as db:
            row = db.execute(
                """
                SELECT id, created_at, report_content, tickers_evaluated
                FROM morning_briefings
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            
            if row:
                return {
                    "id": row[0],
                    "created_at": utc_iso(row[1]),
                    "report_content": row[2],
                    "tickers_evaluated": row[3]
                }
    except Exception as e:
        logger.error("[MORNING BRIEFING] Failed to fetch latest briefing: %s", e)
    return None
