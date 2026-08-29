"""
Morning Briefing Generator — V3 Pipeline
Produces pre-market cross-analysis of active portfolio holdings and watchlist tickers.
Compares recent theses, verdicts, confidence levels, and highlights actionable insights.
"""

import logging
from datetime import datetime, timezone, timedelta

from app.services.prism_agent_caller import llm, Priority, call_prism_agent
from app.db import mongo_query, mongo_store
from app.utils.tz import utc_iso
from app.utils.pg_arrays import as_list
from app.utils.json_utils import parse_json_field as _parse_result_json

logger = logging.getLogger(__name__)


def _next_morning_briefing_id() -> int:
    """Determine the next sequential ID for morning_briefings."""
    try:
        current_max = mongo_query.agg_row('morning_briefings', {}, [("max", "id")])[0]
        return int(current_max) + 1 if current_max is not None else 1
    except Exception as e:
        fallback = int(datetime.now(timezone.utc).timestamp())
        logger.warning(
            "[MORNING BRIEFING] could not read max(id) (%s) — falling back to %d", e, fallback
        )
        return fallback


def _morning_briefing_doc(report_content: str, tickers_evaluated: list) -> dict:
    """Create the document dictionary to persist to morning_briefings collection."""
    return {
        'id': _next_morning_briefing_id(),
        'created_at': datetime.now(timezone.utc),
        'report_content': report_content,
        'tickers_evaluated': tickers_evaluated,
    }


async def generate_morning_briefing() -> str:
    """Generate a morning briefing comparing recent stock analyses across portfolio & watchlist."""
    logger.info("[MORNING BRIEFING] Generating morning briefing...")

    # 1. Gather target universe (Portfolio + Watchlist)
    watchlist_tickers = []
    portfolio_tickers = []
    try:
        wl_rows = mongo_query.find_rows('watchlist', {'status': 'active'}, ['ticker'])
        watchlist_tickers = [r[0] for r in wl_rows if r[0]]
        pos_rows = mongo_query.find_rows('positions', {}, ['ticker'])
        portfolio_tickers = [r[0] for r in pos_rows if r[0]]
    except Exception as e:
        logger.error("[MORNING BRIEFING] Failed to fetch watchlist/portfolio: %s", e)

    target_tickers = list(dict.fromkeys(portfolio_tickers + watchlist_tickers))
    if not target_tickers:
        # Fallback to key benchmark / default tickers if portfolio & watchlist are empty
        target_tickers = ["AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "META", "TSLA"]

    logger.info(
        "[MORNING BRIEFING] Target universe: %d tickers (%s)",
        len(target_tickers),
        ", ".join(target_tickers[:10]),
    )

    # 2. Extract recent analysis results / theses (within the last 5 days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=5)
    context_parts = []
    evaluated_tickers = []

    for ticker in target_tickers:
        try:
            # Look up recent analysis_results
            docs = mongo_store.find_docs(
                "analysis_results",
                {"ticker": ticker, "created_at": {"$gte": cutoff}},
                sort=[("created_at", -1)],
                limit=1,
            )
            if not docs:
                # Fall back to latest analysis_result regardless of cutoff if available
                docs = mongo_store.find_docs(
                    "analysis_results",
                    {"ticker": ticker},
                    sort=[("created_at", -1)],
                    limit=1,
                )

            if docs:
                doc = docs[0]
                result = _parse_result_json(doc.get("result_json"))
                action = result.get("action") or doc.get("action") or "UNKNOWN"
                confidence = doc.get("confidence") or result.get("confidence") or "N/A"
                rationale = result.get("rationale") or doc.get("thesis_summary") or ""
                price = doc.get("price_at_analysis") or ""
                price_str = f" | Price: ${price:.2f}" if isinstance(price, (int, float)) else ""

                context_parts.append(
                    f"## {ticker}\n"
                    f"Action / Verdict: {action}\n"
                    f"Confidence: {confidence}%{price_str}\n"
                    f"Rationale & Analysis: {rationale[:600]}\n"
                )
                evaluated_tickers.append(ticker)
            else:
                # Basic placeholder if no thesis exists yet
                context_parts.append(
                    f"## {ticker}\n"
                    f"Status: Active in Watchlist/Portfolio (Pending deep-dive thesis)\n"
                )
                evaluated_tickers.append(ticker)
        except Exception as e:
            logger.warning("[MORNING BRIEFING] Error fetching thesis for %s: %s", ticker, e)

    if not evaluated_tickers:
        evaluated_tickers = target_tickers

    context = (
        f"# Target Universe & Recent Stock Theses ({len(evaluated_tickers)} Tickers)\n\n"
        + "\n".join(context_parts)
    )

    system_prompt = (
        "You are the Chief Investment Officer (CIO) and Senior Portfolio Strategist. "
        "Given the following latest research theses and signals across our active portfolio holdings and watchlist, "
        "synthesize a comprehensive Morning Briefing Executive Summary for the trading day.\n\n"
        "Structure your report cleanly in Markdown:\n"
        "Based on the provided pipeline data, here is a consolidated summary of the trading recommendations, "
        "highlighting key divergences and actionable insights.\n\n"
        "### **Executive Summary**\n"
        "* **Strongest Buy Signal:** **TICKER** (XX% Confidence) – Key reason...\n"
        "* **Strongest Sell Signal:** **TICKER** (XX% Confidence) – Key reason...\n"
        "* **Consensus Hold:** **TICKER1, TICKER2** – Shared characteristics/rationale...\n"
        "* **Value Buy / Strategic Opportunity:** **TICKER** (XX% Confidence) – Key reason...\n\n"
        "### **Portfolio & Watchlist Insights**\n"
        "Provide 3-5 concise bullet points highlighting key tactical observations, divergences, upcoming catalysts, or risks across the universe.\n\n"
        "Do NOT include conversational preambles, introductory filler, or sign-offs. Output valid Markdown directly."
    )

    # 3. Call LLM agent
    try:
        response, tokens, ms = await call_prism_agent(
            agent_id="CUSTOM_MORNING_BRIEFING_AGENT",
            user_message=context,
            fallback_system_prompt=system_prompt,
            fallback_agent_name="morning_briefing_analyst",
            temperature=0.3,
            max_tokens=8192,
            priority=Priority.HIGH,
        )
    except Exception as e:
        logger.error("[MORNING BRIEFING] LLM call failed: %s", e)
        response = (
            "### **Morning Briefing — Temporary Fallback**\n\n"
            f"Evaluated Universe: {', '.join(evaluated_tickers)}\n\n"
            "Unable to generate full LLM synthesis at this time. Please review individual ticker dossiers."
        )

    # 4. Save to Database
    try:
        mongo_store.insert_docs('morning_briefings', [
            _morning_briefing_doc(response, evaluated_tickers)
        ])
        logger.info(
            "[MORNING BRIEFING] Saved morning briefing (%d tickers evaluated)",
            len(evaluated_tickers),
        )
    except Exception as e:
        logger.error("[MORNING BRIEFING] Failed to save morning briefing to DB: %s", e)

    return response


def get_recent_morning_briefings(limit: int = 10) -> list[dict]:
    """Fetch the most recent morning briefings."""
    try:
        rows = mongo_query.find_rows(
            'morning_briefings',
            {},
            ['id', 'created_at', 'report_content', 'tickers_evaluated'],
            sort=[('created_at', -1)],
            limit=limit,
        )
        return [
            {
                "id": r[0],
                "created_at": utc_iso(r[1]),
                "report_content": r[2],
                "tickers_evaluated": as_list(r[3]),
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("[MORNING BRIEFING] Failed to fetch morning briefings: %s", e)
        return []
