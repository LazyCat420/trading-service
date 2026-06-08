"""
Watchlist Curator — LLM-powered watchlist pruning and curation.

Triggered when a ticker accumulates 3+ HOLD/SELL decisions within a 7-day window.
Instead of hard-coded auto-purge rules, the LLM evaluates the ticker's fundamentals,
technicals, and decision history to decide: KEEP, REMOVE, or NEEDS_MORE_DATA.

Usage:
    from app.cognition.watchlist_curator import evaluate_ticker_for_curation

    result = await evaluate_ticker_for_curation(
        ticker="AAPL",
        recent_decisions=[...],
        cycle_id="cycle_xyz",
    )
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────
HOLD_SELL_TRIGGER_COUNT = 3    # Number of HOLD/SELL decisions to trigger curation
TRIGGER_WINDOW_DAYS = 7        # Time window for counting decisions

CURATOR_SYSTEM_PROMPT = """\
You are a Watchlist Curator for an autonomous stock trading bot. Your job is to
evaluate whether a ticker should remain on the watchlist based on its recent
analysis history, fundamentals, and market conditions.

The bot keeps making HOLD or SELL decisions on this ticker, which means it's not
finding a compelling reason to BUY. You must decide if this ticker is worth
continuing to monitor or if it should be removed to make room for better candidates.

## Decision Framework

**KEEP** — The company is fundamentally sound but the timing/price isn't right yet.
Worth monitoring for a future entry point. Examples:
- Strong balance sheet but stock is overvalued (waiting for pullback)
- Good company in a sector-wide downturn (will recover)
- Upcoming catalyst (earnings, product launch) worth waiting for

**REMOVE** — The company has structural problems or there's no thesis for buying.
Continuing to monitor is a waste of cycles. Examples:
- Declining revenue with no turnaround catalyst
- High debt and deteriorating margins
- No competitive advantage or moat
- Consistently poor analyst sentiment
- Stock has been trending down with no floor in sight

**NEEDS_MORE_DATA** — Cannot make a confident decision with available data.
Flag for deep analysis next cycle. Examples:
- Missing fundamental data (no earnings, no revenue)
- Conflicting signals between technicals and fundamentals
- Recent major news that hasn't been fully analyzed

## Output Format
Respond with ONLY a JSON object:
{
    "decision": "KEEP" | "REMOVE" | "NEEDS_MORE_DATA",
    "rationale": "2-3 sentence explanation of your decision",
    "suggested_tier": "standard" | "glance" | "deep"
}
"""


def should_trigger_curation(recent_decisions: list[dict]) -> bool:
    """Check if a ticker's recent decisions warrant LLM curation.

    Trigger = 3+ HOLD/SELL decisions within the last 7 days.

    Args:
        recent_decisions: List of dicts with 'action', 'confidence', 'at' keys.

    Returns:
        True if curation should be triggered.
    """
    if not recent_decisions or len(recent_decisions) < HOLD_SELL_TRIGGER_COUNT:
        return False

    now = datetime.now(timezone.utc)
    hold_sell_in_window = 0

    for dec in recent_decisions:
        try:
            dec_time = datetime.fromisoformat(dec["at"])
            if dec_time.tzinfo is None:
                dec_time = dec_time.replace(tzinfo=timezone.utc)
            days_ago = (now - dec_time).total_seconds() / 86400
            if days_ago <= TRIGGER_WINDOW_DAYS and dec.get("action") in ("HOLD", "SELL"):
                hold_sell_in_window += 1
        except (KeyError, ValueError, TypeError):
            continue

    return hold_sell_in_window >= HOLD_SELL_TRIGGER_COUNT


async def _build_curator_context(ticker: str, recent_decisions: list[dict]) -> str:
    """Build the evidence context for the LLM curator.

    Gathers: recent decisions, fundamentals, technicals, news.
    """
    sections = []

    # 1. Recent decision history
    dec_lines = []
    for dec in recent_decisions[-5:]:
        dec_lines.append(
            f"- {dec.get('at', '?')}: {dec.get('action', '?')} @ {dec.get('confidence', '?')}%"
        )
    sections.append(
        f"# RECENT DECISIONS FOR {ticker}\n" + "\n".join(dec_lines)
    )

    # 2 & 3. Fundamentals & Technicals snapshot (build dynamically)
    try:
        from app.data.market_data import build_snapshot
        snapshot = await build_snapshot(ticker)
        sections.append(
            f"# FUNDAMENTALS\n"
            f"- P/E Ratio: {snapshot.pe_ratio}\n"
            f"- Revenue Growth: {snapshot.revenue_growth}\n"
            f"- Profit Margin: {snapshot.profit_margin}\n"
            f"- Debt/Equity: {snapshot.debt_to_equity}\n"
            f"- Market Cap: {snapshot.market_cap}\n"
            f"- EPS: {snapshot.eps}"
        )
        sections.append(
            f"# TECHNICALS\n"
            f"- Current Price: ${snapshot.price}\n"
            f"- RSI (14): {snapshot.rsi_14}\n"
            f"- MACD: {snapshot.macd} (Signal: {snapshot.macd_signal})\n"
            f"- SMA 50: {snapshot.sma_50}\n"
            f"- SMA 200: {snapshot.sma_200}\n"
            f"- Returns 1D/5D/20D: {snapshot.returns_1d}% / {snapshot.returns_5d}% / {snapshot.returns_20d}%"
        )
    except Exception as e:
        logger.warning("Failed to build snapshot for curation of %s: %s", ticker, e)
        sections.append(f"# FUNDAMENTALS & TECHNICALS\nFailed to fetch market data: {e}")

    # 4. Recent news
    try:
        with get_db() as db:
            news_rows = db.execute(
                """
                SELECT title, qualitative_draft->>'impact' AS sentiment, published_at
                FROM news_articles
                WHERE ticker = %s
                ORDER BY published_at DESC LIMIT 5
                """,
                [ticker],
            ).fetchall()
            if news_rows:
                news_lines = []
                for title, sentiment, pub_at in news_rows:
                    pub_str = pub_at.strftime("%Y-%m-%d") if pub_at else "?"
                    news_lines.append(f"- [{pub_str}] ({sentiment or '?'}) {title}")
                sections.append("# RECENT NEWS\n" + "\n".join(news_lines))
            else:
                sections.append("# RECENT NEWS\nNo recent news articles.")
    except Exception as e:
        sections.append(f"# RECENT NEWS\nFailed to fetch: {e}")

    return "\n\n".join(sections)


async def evaluate_ticker_for_curation(
    ticker: str,
    recent_decisions: list[dict],
    cycle_id: str = "",
) -> dict:
    """Run the LLM Watchlist Curator on a ticker.

    Args:
        ticker: Stock symbol.
        recent_decisions: Recent decision history from ticker_attention.
        cycle_id: Current cycle ID for audit trail.

    Returns:
        Dict with 'decision', 'rationale', 'suggested_tier' keys.
    """
    context = await _build_curator_context(ticker, recent_decisions)
    user_prompt = (
        f"Evaluate ticker {ticker} for watchlist curation.\n\n"
        f"{context}\n\n"
        "Based on the evidence above, should this ticker KEEP being monitored, "
        "be REMOVED from the watchlist, or does it NEED_MORE_DATA?"
    )

    try:
        from app.core.llm_caller import llm_call

        response_text, tokens = await llm_call(
            system=CURATOR_SYSTEM_PROMPT,
            user=user_prompt,
            agent_name="watchlist_curator",
            timeout=60.0,
            ticker=ticker,
            cycle_id=cycle_id,
        )

        # Parse structured JSON response
        result = _parse_curator_response(response_text)
        logger.info(
            "[CURATOR] %s: %s — %s (tokens: %d)",
            ticker, result["decision"], result["rationale"][:100], tokens,
        )

        # Log to audit table
        _log_curation_decision(ticker, cycle_id, recent_decisions, result)

        return result

    except Exception as e:
        logger.error("[CURATOR] Failed for %s: %s", ticker, e)
        return {
            "decision": "KEEP",
            "rationale": f"Curator evaluation failed ({e}), defaulting to KEEP",
            "suggested_tier": "standard",
        }


def _parse_curator_response(text: str) -> dict:
    """Parse the LLM's JSON response, with fallback for malformed output."""
    # Try to extract JSON from the response
    try:
        # Look for JSON block
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end])
            decision = parsed.get("decision", "KEEP").upper()
            if decision not in ("KEEP", "REMOVE", "NEEDS_MORE_DATA"):
                decision = "KEEP"
            return {
                "decision": decision,
                "rationale": parsed.get("rationale", "No rationale provided"),
                "suggested_tier": parsed.get("suggested_tier", "standard"),
            }
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: try to detect decision from text
    text_upper = text.upper()
    if "REMOVE" in text_upper:
        decision = "REMOVE"
    elif "NEEDS_MORE_DATA" in text_upper:
        decision = "NEEDS_MORE_DATA"
    else:
        decision = "KEEP"

    return {
        "decision": decision,
        "rationale": text[:200] if text else "Failed to parse curator response",
        "suggested_tier": "standard",
    }


def _log_curation_decision(
    ticker: str,
    cycle_id: str,
    recent_decisions: list[dict],
    result: dict,
) -> None:
    """Write curation decision to the audit log table."""
    try:
        log_id = str(uuid.uuid4())
        with get_db() as db:
            db.execute(
                """
                INSERT INTO watchlist_curation_log
                (id, ticker, cycle_id, trigger_reason, decision, rationale,
                 suggested_tier, recent_decisions)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    log_id,
                    ticker,
                    cycle_id or "manual",
                    f"{HOLD_SELL_TRIGGER_COUNT}+ HOLD/SELL decisions in {TRIGGER_WINDOW_DAYS} days",
                    result["decision"],
                    result["rationale"],
                    result.get("suggested_tier", "standard"),
                    json.dumps(recent_decisions[-5:]),
                ],
            )
    except Exception as e:
        logger.warning("[CURATOR] Failed to log curation decision for %s: %s", ticker, e)


async def apply_curation_decision(
    ticker: str,
    result: dict,
) -> None:
    """Execute the curator's decision on the watchlist.

    - KEEP → no action (optionally adjust triage tier)
    - REMOVE → auto_purge_ticker with the curator's rationale
    - NEEDS_MORE_DATA → flag for deep analysis
    """
    decision = result.get("decision", "KEEP")
    rationale = result.get("rationale", "")

    if decision == "REMOVE":
        from app.trading.watchlist import auto_purge_ticker

        purged = auto_purge_ticker(ticker, reason=f"Curator: {rationale[:200]}")
        if purged:
            logger.info("[CURATOR] REMOVED %s from watchlist: %s", ticker, rationale[:100])
        else:
            logger.info("[CURATOR] %s already removed/not on watchlist", ticker)

    elif decision == "NEEDS_MORE_DATA":
        # Flag for deep analysis next cycle by resetting days_since_deep to max
        try:
            with get_db() as db:
                db.execute(
                    "UPDATE ticker_attention SET days_since_deep = 999 WHERE ticker = %s",
                    [ticker],
                )
            logger.info("[CURATOR] Flagged %s for DEEP analysis next cycle", ticker)
        except Exception as e:
            logger.warning("[CURATOR] Failed to flag %s for deep: %s", ticker, e)

    elif decision == "KEEP":
        suggested_tier = result.get("suggested_tier", "standard")
        logger.info(
            "[CURATOR] KEEPING %s on watchlist (suggested tier: %s)",
            ticker, suggested_tier,
        )
    else:
        logger.warning("[CURATOR] Unknown decision '%s' for %s, defaulting to KEEP", decision, ticker)
