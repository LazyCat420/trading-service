"""
Discovery Mode — Finds new stock leads when the Freshness Gate
determines all current stocks are STALE (< 3 eligible).

Leverages existing infrastructure:
  1. Reddit Purge (fresh scrape via scraper-service)
  2. discovered_tickers table (populated by Reddit/YouTube collectors)
  3. news_articles DB (last 12h trending tickers)
  4. reddit_posts DB (last 12h trending tickers)
  5. Institutional fund scanner (hedge fund consensus)
  6. Web search (lazy_web_search fallback)

No new external dependencies — just wires existing collectors into the pipeline.
"""

import logging
import asyncio
from datetime import datetime, timezone

from app.db.connection import get_db
from app.processors.ticker_extractor import FALSE_TICKERS

logger = logging.getLogger(__name__)

MAX_DISCOVERY_TICKERS = 10


async def run_discovery(
    existing_tickers: list[str],
    emit: object = None,
) -> list[dict]:
    """Find new stock leads from existing data sources.

    Args:
        existing_tickers: Tickers already in the Top 20 (to deduplicate).
        emit: Optional SSE emitter for real-time logging.

    Returns:
        List of new stock dicts with at least {ticker, score, src, freshness}.
    """
    existing_set = set(t.upper() for t in existing_tickers)
    source_tracker: dict[str, dict] = {}  # ticker -> {sources: set, mentions: int}

    logger.info("[DiscoveryMode] Starting — need new leads (existing: %d tickers)", len(existing_set))

    # ── Source 1: Fresh Reddit Purge (scrape NOW) ──
    try:
        from app.collectors.reddit_collector import run_reddit_purge_discovery
        count = await run_reddit_purge_discovery(limit=15)
        if count:
            logger.info("[DiscoveryMode] Reddit Purge: discovered %d tickers", count)
    except Exception as e:
        logger.warning("[DiscoveryMode] Reddit Purge failed (non-fatal): %s", e)

    # ── Source 2: discovered_tickers table (populated by Reddit/YouTube) ──
    try:
        with get_db() as db:
            rows = db.execute("""
                SELECT ticker, score, context FROM discovered_tickers
                WHERE discovered_at > NOW() - INTERVAL '24 hours'
                  AND (validation_status IS NULL OR validation_status != 'rejected')
                ORDER BY score DESC
                LIMIT 20
            """).fetchall()
            for ticker, score, context in rows:
                tkr = ticker.upper().strip()
                if tkr in existing_set or tkr in FALSE_TICKERS:
                    continue
                if tkr not in source_tracker:
                    source_tracker[tkr] = {"sources": set(), "mentions": 0}
                source_tracker[tkr]["sources"].add("discovered_tickers")
                source_tracker[tkr]["mentions"] += score or 1
            if rows:
                logger.info("[DiscoveryMode] discovered_tickers: %d candidates", len(rows))
    except Exception as e:
        logger.warning("[DiscoveryMode] discovered_tickers query failed: %s", e)

    # ── Source 3: News articles (last 12h, 3+ mentions) ──
    try:
        with get_db() as db:
            rows = db.execute("""
                SELECT ticker, COUNT(*) as mentions
                FROM news_articles
                WHERE ticker IS NOT NULL
                  AND published_at > NOW() - INTERVAL '12 hours'
                GROUP BY ticker
                HAVING COUNT(*) >= 3
                ORDER BY COUNT(*) DESC
                LIMIT 15
            """).fetchall()
            for ticker, mentions in rows:
                tkr = ticker.upper().strip()
                if tkr in existing_set or tkr in FALSE_TICKERS:
                    continue
                if tkr not in source_tracker:
                    source_tracker[tkr] = {"sources": set(), "mentions": 0}
                source_tracker[tkr]["sources"].add("News")
                source_tracker[tkr]["mentions"] += mentions
            if rows:
                logger.info("[DiscoveryMode] News articles: %d trending tickers", len(rows))
    except Exception as e:
        logger.warning("[DiscoveryMode] News query failed: %s", e)

    # ── Source 4: Reddit posts (last 12h, 3+ mentions) ──
    try:
        with get_db() as db:
            rows = db.execute("""
                SELECT ticker, COUNT(*) as mentions
                FROM reddit_posts
                WHERE ticker IS NOT NULL
                  AND created_utc > NOW() - INTERVAL '12 hours'
                GROUP BY ticker
                HAVING COUNT(*) >= 3
                ORDER BY COUNT(*) DESC
                LIMIT 15
            """).fetchall()
            for ticker, mentions in rows:
                tkr = ticker.upper().strip()
                if tkr in existing_set or tkr in FALSE_TICKERS:
                    continue
                if tkr not in source_tracker:
                    source_tracker[tkr] = {"sources": set(), "mentions": 0}
                source_tracker[tkr]["sources"].add("Reddit")
                source_tracker[tkr]["mentions"] += mentions
            if rows:
                logger.info("[DiscoveryMode] Reddit posts: %d trending tickers", len(rows))
    except Exception as e:
        logger.warning("[DiscoveryMode] Reddit query failed: %s", e)

    # ── Source 5: Institutional consensus ──
    try:
        from app.collectors.fund_scanner import get_top_conviction_tickers
        leads = get_top_conviction_tickers(min_funds=2, max_results=10)
        for lead in leads:
            tkr = lead["ticker"].upper().strip()
            if tkr in existing_set or tkr in FALSE_TICKERS:
                continue
            if tkr not in source_tracker:
                source_tracker[tkr] = {"sources": set(), "mentions": 0}
            source_tracker[tkr]["sources"].add("Institutional")
            source_tracker[tkr]["mentions"] += lead.get("fund_count", 1)
        if leads:
            logger.info("[DiscoveryMode] Institutional: %d conviction leads", len(leads))
    except Exception as e:
        logger.warning("[DiscoveryMode] Institutional scan failed: %s", e)

    # ── Filter: US-tradeable only ──
    try:
        from app.validation.ticker_validator import is_us_tradeable
    except ImportError:
        def is_us_tradeable(t):
            return True  # Fallback if validator not available

    valid_candidates = {}
    for tkr, info in source_tracker.items():
        if not is_us_tradeable(tkr):
            logger.debug("[DiscoveryMode] Filtered non-US ticker: %s", tkr)
            continue
        valid_candidates[tkr] = info

    # ── Rank by: multi-source first, then mention count ──
    ranked = sorted(
        valid_candidates.items(),
        key=lambda x: (len(x[1]["sources"]), x[1]["mentions"]),
        reverse=True,
    )

    # ── Build result list ──
    discoveries = []
    for tkr, info in ranked[:MAX_DISCOVERY_TICKERS]:
        source_label = "Discovery Mode (" + "+".join(sorted(info["sources"])) + ")"
        discoveries.append({
            "ticker": tkr,
            "score": info["mentions"],
            "src": source_label,
            "dsa": "Never",
            "price": 0,
            "chg": 0,
            "rvol": 0,
            "sma": 0,
            "rsi": 50,
            "inst_funds": 0,
            "freshness": "NEW",
            "delta_score": 1.0,
            "freshness_reason": f"discovered via {source_label}",
        })

    logger.info(
        "[DiscoveryMode] Found %d new leads: %s",
        len(discoveries),
        [d["ticker"] for d in discoveries],
    )

    # ── Source 6 (Fallback): If still < 3 leads, try web search ──
    if len(discoveries) < 3:
        logger.info("[DiscoveryMode] Only %d leads — trying web search fallback", len(discoveries))
        try:
            web_leads = await _web_search_fallback(existing_set, set(d["ticker"] for d in discoveries))
            discoveries.extend(web_leads)
            logger.info("[DiscoveryMode] Web search added %d leads", len(web_leads))
        except Exception as e:
            logger.warning("[DiscoveryMode] Web search fallback failed: %s", e)

    return discoveries[:MAX_DISCOVERY_TICKERS]


async def _web_search_fallback(
    existing_tickers: set,
    already_discovered: set,
) -> list[dict]:
    """Fallback: use lazy_web_search to find trending movers."""
    import re

    try:
        from app.tools.registry import registry
        result = await registry.call_tool(
            "lazy_web_search",
            {"query": "stock market movers today biggest gainers losers 2026"},
        )
        if not result:
            return []

        text = str(result)
        # Extract potential ticker symbols (1-5 uppercase letters)
        raw_tickers = re.findall(r'\b([A-Z]{1,5})\b', text)

        # Filter
        candidates = []
        seen = set()
        for t in raw_tickers:
            if t in existing_tickers or t in already_discovered or t in FALSE_TICKERS:
                continue
            if t in seen or len(t) < 2:
                continue
            seen.add(t)
            candidates.append({
                "ticker": t,
                "score": 1,
                "src": "Discovery Mode (Web Search)",
                "dsa": "Never",
                "price": 0,
                "chg": 0,
                "rvol": 0,
                "sma": 0,
                "rsi": 50,
                "inst_funds": 0,
                "freshness": "NEW",
                "delta_score": 1.0,
                "freshness_reason": "discovered via web search",
            })
        return candidates[:5]
    except Exception as e:
        logger.warning("[DiscoveryMode] Web search extraction failed: %s", e)
        return []
