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

    # Congress disclosures. 30k rows and by far the deepest alt-data set we
    # hold, but it was the one domain with no reader on the per-ticker path —
    # it reached ticker SELECTION via _inject_smart_money_leads and then
    # vanished before the desk ever reasoned about it.
    #
    # trade_date <= CURRENT_DATE because the table carries future-dated rows
    # (max was 2026-12-26 on 2026-07-27, open since the 07-23 audit); without
    # the guard a bad row would present as the most recent disclosure.
    try:
        with get_db() as db:
            rows = db.execute(
                """
                SELECT transaction_type, COUNT(*), MAX(trade_date), MAX(politician)
                FROM congress_trades
                WHERE ticker = %s
                  AND trade_date >= CURRENT_DATE - INTERVAL '90 days'
                  AND trade_date <= CURRENT_DATE
                GROUP BY transaction_type
                ORDER BY COUNT(*) DESC
                """,
                [ticker],
            ).fetchall()
        if rows:
            detail = ", ".join(
                f"{r[1]}× {r[0] or 'unknown'} (latest {r[2]}, e.g. {r[3]})"
                for r in rows[:3]
            )
            parts.append(
                f"- Congressional disclosures (90d): {detail}. Disclosure lags "
                f"the trade by up to 45 days — treat as slow confirmation, "
                f"never as a timing signal."
            )
    except Exception as e:
        logger.debug("[AltDataBlock] %s: congress query failed (non-fatal): %s", ticker, e)

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


# ── The consumption half (2026-07-28) ────────────────────────────────────────
#
# The block above was widened from 2 agents to 6 and then MEASURED: zero of the
# newly-added agents cited it. Injection alone is not enough — optional context
# loses to a 7,962-char compressed desk view every time.
#
# What worked for fundamentals was three things together, not one: the block,
# a REQUIRED schema field, and a reconcile pass that overwrites and counts
# disagreement. That took the fundamental desk from 0 numeric fields to 23
# reconciled ones. This is the same shape for positioning evidence.
#
# The counts below are the verifiable half. The agent's READ of them
# (bullish/bearish/neutral) is judgment and is never touched — the same
# boundary every other reconcile pass holds.

VERIFIED_POSITIONING_FIELDS = (
    "insider_buy_filings_30d",
    "congress_disclosures_90d",
    "social_posts_7d",
)


def compute_positioning_facts(ticker: str) -> dict:
    """The countable facts behind the alt-data block, as a dict.

    Same queries as `build_alt_data_block`, returning numbers instead of prose
    so an artifact claim can be checked against them. Absent evidence is 0, not
    None: "no congressional disclosures" is a fact about the world, whereas a
    missing multiple is a gap in ours.
    """
    ticker = (ticker or "").strip().upper()
    facts = {f: 0 for f in VERIFIED_POSITIONING_FIELDS}
    if not ticker:
        return facts

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM insider_trades WHERE ticker = %s "
                "AND trade_type = 'P' "
                "AND trade_date >= CURRENT_DATE - INTERVAL '30 days'",
                [ticker],
            ).fetchone()
            facts["insider_buy_filings_30d"] = int(row[0]) if row else 0
    except Exception as e:
        logger.debug("[AltData] %s: insider facts failed: %s", ticker, e)

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM congress_trades WHERE ticker = %s "
                "AND trade_date >= CURRENT_DATE - INTERVAL '90 days' "
                "AND trade_date <= CURRENT_DATE",
                [ticker],
            ).fetchone()
            facts["congress_disclosures_90d"] = int(row[0]) if row else 0
    except Exception as e:
        logger.debug("[AltData] %s: congress facts failed: %s", ticker, e)

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM social_posts WHERE ticker = %s "
                "AND posted_at >= NOW() - INTERVAL '7 days'",
                [ticker],
            ).fetchone()
            facts["social_posts_7d"] = int(row[0]) if row else 0
    except Exception as e:
        logger.debug("[AltData] %s: social facts failed: %s", ticker, e)

    return facts


def reconcile_positioning_read(artifact: dict, ticker: str) -> dict:
    """Overwrite the counts inside `positioning_read`; keep the model's read.

    Exact-match, not tolerance-based: these are integer counts of filings, so
    "6" and "7" are different facts, not a rounding difference.

    `stance` and `note` are the agent's judgment and are NEVER touched — a
    module that counts filings has no opinion on what they mean.
    """
    if not isinstance(artifact, dict):
        return {}
    block = artifact.get("positioning_read")
    if not isinstance(block, dict):
        return {}

    facts = compute_positioning_facts(ticker)
    corrected: dict = {}
    original: dict = {}

    for field in VERIFIED_POSITIONING_FIELDS:
        verified = facts.get(field)
        if verified is None:
            continue
        stated = block.get(field)
        try:
            stated_i = int(stated)
        except (TypeError, ValueError):
            stated_i = None
        if stated_i != verified:
            if stated_i is not None:
                corrected[field] = {"model": stated_i, "verified": verified}
                original[field] = stated_i
            block[field] = verified

    if original:
        artifact["_model_reported_positioning"] = original
        # The stance was reasoned from the numbers we just replaced, so it is
        # now downstream of a fact the agent did not have. Seen on the first
        # live run: AAPL reported congress_disclosures_90d = 0 against a true
        # 8, and concluded "NO_COVERAGE" — the count was corrected and the
        # conclusion built on it was not.
        #
        # We do NOT rewrite the stance: judgment is the agent's job and this
        # module counts filings. What it can do is stop the stale conclusion
        # travelling as though it were founded, so the desk render and any
        # downstream reader can discount it.
        block["stance_is_stale"] = True
        block["stance_stale_reason"] = (
            "derived from counts that were corrected: "
            + ", ".join(
                f"{f} stated {v}, actual {facts.get(f)}"
                for f, v in original.items()
            )
        )

    return {"corrected": corrected, "facts": facts}
