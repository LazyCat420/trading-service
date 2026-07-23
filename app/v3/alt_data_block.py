"""Precomputed alternative-data context block.

The 2026-07-23 collector wave started filling insider_trades, social_posts,
put_call_ratio and economic_calendar — tables that previously had zero rows
and zero readers. Same design as app/quant/context_block.py: telemetry shows
the analysts rarely make optional tool calls, so the signal is computed in
code at desk build and injected into their prompts.

Everything is fail-open: any exception degrades to a missing line or an
empty block, never a pipeline error.
"""

from __future__ import annotations

import logging

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def build_alt_data_block(ticker: str) -> str:
    """Insider cluster-buys + social chatter for one ticker. "" when quiet."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return ""
    parts: list[str] = []

    try:
        with get_db() as db:
            row = db.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(value), 0), MAX(trade_date),
                       MAX(insider_name)
                FROM insider_trades
                WHERE ticker = %s
                  AND trade_type = 'P'
                  AND trade_date >= CURRENT_DATE - INTERVAL '30 days'
                """,
                [ticker],
            ).fetchone()
        if row and row[0]:
            parts.append(
                f"- Insider cluster buying (30d): {row[0]} filing(s) totaling "
                f"${row[1]:,.0f}, most recent {row[2]} ({row[3]}). Cluster buys "
                f"(multiple insiders) are among the strongest insider signals."
            )
    except Exception as e:
        logger.debug("[AltDataBlock] %s: insider query failed (non-fatal): %s", ticker, e)

    try:
        with get_db() as db:
            row = db.execute(
                """
                SELECT COUNT(*), AVG(sentiment_score),
                       COALESCE(SUM(COALESCE(like_count,0) + COALESCE(repost_count,0)), 0)
                FROM social_posts
                WHERE ticker = %s
                  AND posted_at >= NOW() - INTERVAL '7 days'
                """,
                [ticker],
            ).fetchone()
        if row and row[0]:
            sent = f", avg sentiment {row[1]:+.2f}" if row[1] is not None else ""
            parts.append(
                f"- Social chatter (7d): {row[0]} posts{sent}, "
                f"{row[2]:,} total engagements. Treat as crowd positioning, not truth."
            )
    except Exception as e:
        logger.debug("[AltDataBlock] %s: social query failed (non-fatal): %s", ticker, e)

    if not parts:
        return ""
    return "## ALTERNATIVE DATA (code-computed — verify, don't re-fetch)\n" + "\n".join(parts)


def alt_macro_lines() -> list[str]:
    """SPY put/call + upcoming high-importance US macro events, for the
    regime briefing. Empty list when the tables are quiet."""
    lines: list[str] = []

    try:
        with get_db() as db:
            row = db.execute(
                """
                SELECT date, pcr_volume, pcr_oi FROM put_call_ratio
                WHERE symbol = 'SPY' ORDER BY date DESC LIMIT 1
                """
            ).fetchone()
        if row and row[1] is not None:
            lines.append(
                f"- SPY put/call ratio ({row[0]}): volume {row[1]:.2f}, "
                f"open-interest {row[2]:.2f} (>1 = defensive positioning, <0.7 = complacency)"
            )
    except Exception as e:
        logger.debug("[AltDataBlock] PCR line failed (non-fatal): %s", e)

    try:
        with get_db() as db:
            rows = db.execute(
                """
                SELECT event_date, event_name, forecast, previous
                FROM economic_calendar
                WHERE country IN ('US', 'USD')
                  AND importance = 'high'
                  AND event_date >= NOW()
                  AND event_date <= NOW() + INTERVAL '7 days'
                ORDER BY event_date ASC LIMIT 5
                """
            ).fetchall()
        if rows:
            lines.append("Upcoming high-impact US events (7d):")
            for r in rows:
                extras = []
                if r[2] is not None:
                    extras.append(f"forecast {r[2]}")
                if r[3] is not None:
                    extras.append(f"prev {r[3]}")
                suffix = f" ({', '.join(extras)})" if extras else ""
                lines.append(f"- {r[0]:%Y-%m-%d %H:%M} UTC: {r[1]}{suffix}")
    except Exception as e:
        logger.debug("[AltDataBlock] calendar lines failed (non-fatal): %s", e)

    return lines
