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

from app.db import mongo_query
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def build_alt_data_block(ticker: str) -> str:
    """Insider cluster-buys + social chatter for one ticker. "" when quiet."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return ""
    parts: list[str] = []

    from datetime import date, datetime, timedelta, timezone
    from app.db import mongo_store

    today = date.today()
    cutoff_30d = today - timedelta(days=30)
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff_90d = today - timedelta(days=90)

    try:
        insider_docs = mongo_store.find_docs(
            "insider_trades",
            {"ticker": ticker, "trade_type": "P", "trade_date": {"$gte": cutoff_30d}},
            sort=[("trade_date", -1)],
        )
        if insider_docs:
            count = len(insider_docs)
            total_val = sum(float(d.get("value") or 0) for d in insider_docs)
            latest_date = insider_docs[0].get("trade_date")
            latest_insider = insider_docs[0].get("insider_name")
            parts.append(
                f"- Insider cluster buying (30d): {count} filing(s) totaling "
                f"${total_val:,.0f}, most recent {latest_date} ({latest_insider}). Cluster buys "
                f"(multiple insiders) are among the strongest insider signals."
            )
    except Exception as e:
        logger.debug("[AltDataBlock] %s: insider query failed (non-fatal): %s", ticker, e)

    try:
        social_docs = mongo_store.find_docs(
            "social_posts",
            {"ticker": ticker, "posted_at": {"$gte": cutoff_7d}},
        )
        if social_docs:
            count = len(social_docs)
            sentiments = [float(d["sentiment_score"]) for d in social_docs if d.get("sentiment_score") is not None]
            avg_sent = sum(sentiments) / len(sentiments) if sentiments else None
            total_eng = sum(int(d.get("like_count") or 0) + int(d.get("repost_count") or 0) for d in social_docs)
            sent_str = f", avg sentiment {avg_sent:+.2f}" if avg_sent is not None else ""
            parts.append(
                f"- Social chatter (7d): {count} posts{sent_str}, "
                f"{total_eng:,} total engagements. Treat as crowd positioning, not truth."
            )
    except Exception as e:
        logger.debug("[AltDataBlock] %s: social query failed (non-fatal): %s", ticker, e)

    try:
        congress_docs = mongo_store.find_docs(
            "congress_trades",
            {"ticker": ticker, "trade_date": {"$gte": cutoff_90d, "$lte": today}},
            sort=[("trade_date", -1)],
        )
        if congress_docs:
            from collections import defaultdict
            by_type = defaultdict(list)
            for d in congress_docs:
                tt = d.get("transaction_type") or "unknown"
                by_type[tt].append(d)
            rows = sorted(by_type.items(), key=lambda x: len(x[1]), reverse=True)
            detail = ", ".join(
                f"{len(docs)}× {tt} (latest {docs[0].get('trade_date')}, e.g. {docs[0].get('politician')})"
                for tt, docs in rows[:3]
            )
            parts.append(
                f"- Congressional activity (90d): {len(congress_docs)} trade(s) — "
                f"{detail}. Disclosures lag 15-45d; treats as thematic positioning, not alpha."
            )
    except Exception as e:
        logger.debug("[AltDataBlock] %s: congress query failed (non-fatal): %s", ticker, e)

    sq = smart_money_quality_line(ticker)
    if sq:
        parts.append(sq)

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
        from app.db import mongo_store

        today = date.today()
        cutoff = today - timedelta(days=_QUALITY_WINDOW_DAYS)

        cohort_docs = mongo_store.find_docs(
            "smart_money_performance",
            {"horizon": "1y", "rankable": True, "avg_alpha": {"$ne": None}},
        )
        if not cohort_docs:
            return ""

        by_type: dict[str, list[float]] = {}
        for d in cohort_docs:
            at = d.get("actor_type")
            alpha = float(d["avg_alpha"])
            by_type.setdefault(at, []).append(alpha)

        pctile_by_actor: dict[tuple[str, str], float] = {}
        cohort_n: dict[str, int] = {}
        for at, alphas in by_type.items():
            alphas_sorted = sorted(alphas)
            n_tot = len(alphas_sorted)
            cohort_n[at] = n_tot
            for d in cohort_docs:
                if d.get("actor_type") == at:
                    aid = d.get("actor_id")
                    alpha = float(d["avg_alpha"])
                    rank = sum(1 for a in alphas_sorted if a < alpha)
                    pctile_by_actor[(at, aid)] = rank / max(1, n_tot - 1)

        score_docs = mongo_store.find_docs(
            "smart_money_trade_scores",
            {"ticker": ticker, "event_date": {"$gte": cutoff, "$lte": today}},
        )
        if not score_docs:
            return ""

        actors_map: dict[tuple[str, str, str], float] = {}
        for s in score_docs:
            at = s.get("actor_type")
            aid = s.get("actor_id")
            dirn = s.get("direction") or "unknown"
            key = (at, aid)
            if key in pctile_by_actor:
                actors_map[(at, aid, dirn)] = pctile_by_actor[key]

        grouped: dict[tuple[str, str], list[float]] = {}
        for (at, aid, dirn), pct in actors_map.items():
            grouped.setdefault((dirn, at), []).append(pct)

        rows = []
        for (dirn, at), pcts in grouped.items():
            pcts_sorted = sorted(pcts)
            mid = len(pcts_sorted) // 2
            med = pcts_sorted[mid] if len(pcts_sorted) % 2 != 0 else (pcts_sorted[mid - 1] + pcts_sorted[mid]) / 2.0
            rows.append((dirn, at, len(pcts), med, cohort_n.get(at, len(pcts))))
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
        f"{_ordinal(round(med * 100))} percentile of their cohort (n={cohort_count})"
        for direction, actor_type, n, med, cohort_count in rows
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
        from app.db import mongo_query, mongo_store
        row = mongo_query.find_row('put_call_ratio', {'symbol': 'SPY'}, ['date', 'pcr_volume', 'pcr_oi'], sort=[('date', -1)])
        if row and row[1] is not None:
            lines.append(
                f"- SPY put/call ratio ({row[0]}): volume {row[1]:.2f}, "
                f"open-interest {row[2]:.2f} (>1 = defensive positioning, <0.7 = complacency)"
            )
    except Exception as e:
        logger.debug("[AltDataBlock] PCR line failed (non-fatal): %s", e)

    try:
        from app.db import mongo_query
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

VERIFIED_POSITIONING_FIELDS = (
    "insider_buy_filings_30d",
    "congress_disclosures_90d",
    "social_posts_7d",
)


def compute_positioning_facts(ticker: str) -> dict:
    """The countable facts behind the alt-data block, as a dict."""
    ticker = (ticker or "").strip().upper()
    facts = {f: 0 for f in VERIFIED_POSITIONING_FIELDS}
    if not ticker:
        return facts

    from datetime import date, datetime, timedelta, timezone
    from app.db import mongo_store

    today = date.today()
    cutoff_30d = today - timedelta(days=30)
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff_90d = today - timedelta(days=90)

    try:
        facts["insider_buy_filings_30d"] = mongo_store.count_docs(
            "insider_trades",
            {"ticker": ticker, "trade_type": "P", "trade_date": {"$gte": cutoff_30d}},
        )
    except Exception as e:
        logger.debug("[AltData] %s: insider facts failed: %s", ticker, e)

    try:
        facts["congress_disclosures_90d"] = mongo_store.count_docs(
            "congress_trades",
            {"ticker": ticker, "trade_date": {"$gte": cutoff_90d, "$lte": today}},
        )
    except Exception as e:
        logger.debug("[AltData] %s: congress facts failed: %s", ticker, e)

    try:
        facts["social_posts_7d"] = mongo_store.count_docs(
            "social_posts",
            {"ticker": ticker, "posted_at": {"$gte": cutoff_7d}},
        )
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
