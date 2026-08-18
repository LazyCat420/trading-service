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
from app.db import mongo_query
from datetime import datetime, timedelta, timezone

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

    quality = smart_money_quality_line(ticker)
    if quality:
        parts.append(quality)

    if not parts:
        return ""
    return "## ALTERNATIVE DATA (code-computed — verify, don't re-fetch)\n" + "\n".join(parts)


_QUALITY_WINDOW_DAYS = 180


def _ordinal(n: int) -> str:
    """1 -> 1st, 13 -> 13th, 72 -> 72nd. The teens are all -th."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def smart_money_quality_line(ticker: str) -> str:
    """How good are the actors who traded this ticker — the weighting fact.

    `smart_money_trade_scores` (79k rows, recomputed daily) and
    `smart_money_performance` were computed for three registered tools that
    were whitelisted to NO agent, so nothing on the desk path had ever read
    them. The disclosure counts above say WHO traded; they cannot say whether
    those people have ever been right, which is what should move a weighting.

    Reports a WITHIN-COHORT PERCENTILE, never the raw alpha, because the raw
    alpha is not a usable measure of skill (measured 2026-08-03):

      * The fund cohort is the 26 hand-picked CIKs in sec_collector's
        TRACKED_FUNDS — selected BECAUSE they are famous and successful, with
        no dead funds. Their measured alpha runs +4 to +8pp over 1,500+ scored
        trades each (Millennium +8.3/n=1867, Citadel +6.9/n=2113). Sustained
        7pp alpha over 1,800 trades is not a plausible skill estimate; it is
        selection plus a mega-cap tilt against SPY over 2012-2025.
      * Reporting that raw number per ticker made every mega-cap read
        "+2 to +5.7pp, buyers and sellers alike" — the cohort's shared bias,
        not anything about the ticker. Buy and sell sides were within 0.1pp of
        each other on NVDA, which is the tell.

    A percentile rank inside the actor's OWN cohort cancels any bias shared by
    that cohort, and it does discriminate: T's congress buyers sit at the 13th
    percentile while MSTR's sits at the 72nd. It is still a relative statement
    among tracked actors, never proof of skill. Returns "" when quiet.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return ""
    try:
        with get_db() as db:
            rows = db.execute(
                """
                WITH cohort AS (
                    SELECT actor_type, actor_id,
                           PERCENT_RANK() OVER (
                               PARTITION BY actor_type ORDER BY avg_alpha
                           ) AS pctile
                    FROM smart_money_performance
                    WHERE horizon = '1y' AND rankable AND avg_alpha IS NOT NULL
                ),
                sized AS (
                    SELECT actor_type, COUNT(*) AS cohort_n
                    FROM cohort GROUP BY actor_type
                ),
                actors AS (
                    SELECT DISTINCT s.actor_type, s.actor_id, s.direction, c.pctile
                    FROM smart_money_trade_scores s
                    JOIN cohort c
                      ON c.actor_type = s.actor_type AND c.actor_id = s.actor_id
                    WHERE s.ticker = %s
                      AND s.event_date >= CURRENT_DATE - MAKE_INTERVAL(days => %s)
                      -- congress_trades carries future-dated rows and the
                      -- scores inherit them. Same guard as the congress query.
                      AND s.event_date <= CURRENT_DATE
                )
                SELECT a.direction, a.actor_type, COUNT(*) AS n,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY a.pctile) AS med,
                       MAX(z.cohort_n) AS cohort_n
                FROM actors a
                JOIN sized z ON z.actor_type = a.actor_type
                GROUP BY a.direction, a.actor_type
                ORDER BY n DESC
                """,
                [ticker, _QUALITY_WINDOW_DAYS],
            ).fetchall()
    except Exception as e:
        logger.debug(
            "[AltDataBlock] %s: smart-money quality query failed (non-fatal): %s",
            ticker, e,
        )
        return ""

    if not rows:
        return ""

    segments = [
        f"{n} {(direction or 'unknown').lower()}-side "
        f"{'fund' if actor_type == 'fund' else 'congress'} actor(s) at the "
        f"{_ordinal(round(med * 100))} percentile of their cohort (n={cohort_n})"
        for direction, actor_type, n, med, cohort_n in rows
        if n and med is not None
    ]
    if not segments:
        return ""

    return (
        f"- Smart-money actor quality ({_QUALITY_WINDOW_DAYS}d): "
        + "; ".join(segments)
        + ". Percentile ranks each actor's 1y alpha against others of the SAME "
        "type, because absolute alphas are not comparable — the fund cohort is "
        "26 hand-picked survivors and its raw alpha is inflated by that "
        "selection. A high percentile means 'better than peers we also track', "
        "not 'skilled'; use it to weight the disclosures above, never alone."
    )


def alt_macro_lines() -> list[str]:
    """SPY put/call + upcoming high-importance US macro events, for the
    regime briefing. Empty list when the tables are quiet."""
    lines: list[str] = []

    try:
        with get_db() as db:
            row = mongo_query.find_row('put_call_ratio', {'symbol': 'SPY'}, ['date', 'pcr_volume', 'pcr_oi'], sort=[('date', -1)])
        if row and row[1] is not None:
            lines.append(
                f"- SPY put/call ratio ({row[0]}): volume {row[1]:.2f}, "
                f"open-interest {row[2]:.2f} (>1 = defensive positioning, <0.7 = complacency)"
            )
    except Exception as e:
        logger.debug("[AltDataBlock] PCR line failed (non-fatal): %s", e)

    try:
        with get_db() as db:
            rows = mongo_query.find_rows('economic_calendar', {'country': {'$in': ['US', 'USD']}, 'importance': 'high', 'event_date': {'$gte': datetime.now(timezone.utc), '$lte': (datetime.now(timezone.utc) + timedelta(days=7))}}, ['event_date', 'event_name', 'forecast', 'previous'], sort=[('event_date', 1)], limit=5)
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
            row = mongo_query.agg_row('insider_trades', {'ticker': ticker, 'trade_type': 'P', 'trade_date': {'$gte': (datetime.now(timezone.utc) - timedelta(days=30))}}, [('count', None)])
            facts["insider_buy_filings_30d"] = int(row[0]) if row else 0
    except Exception as e:
        logger.debug("[AltData] %s: insider facts failed: %s", ticker, e)

    try:
        with get_db() as db:
            row = mongo_query.agg_row('congress_trades', {'ticker': ticker, 'trade_date': {'$gte': (datetime.now(timezone.utc) - timedelta(days=90)), '$lte': datetime.now(timezone.utc)}}, [('count', None)])
            facts["congress_disclosures_90d"] = int(row[0]) if row else 0
    except Exception as e:
        logger.debug("[AltData] %s: congress facts failed: %s", ticker, e)

    try:
        with get_db() as db:
            row = mongo_query.agg_row('social_posts', {'ticker': ticker, 'posted_at': {'$gte': (datetime.now(timezone.utc) - timedelta(days=7))}}, [('count', None)])
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
