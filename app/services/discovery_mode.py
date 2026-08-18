"""
Discovery Mode — Finds new stock leads when the Freshness Gate
determines all current stocks are STALE (< 3 eligible).

Pure MongoDB implementation for discovered_tickers, news_articles, reddit_posts.
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta

from app.processors.ticker_extractor import FALSE_TICKERS
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

MAX_DISCOVERY_TICKERS = 10


async def run_discovery(
    existing_tickers: list[str],
    emit: object = None,
) -> list[dict]:
    """Find new stock leads from existing data sources."""
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
        rows = mongo_query.find_rows(
            'discovered_tickers',
            {
                'discovered_at': {'$gt': (datetime.now(timezone.utc) - timedelta(hours=24))},
                '$or': [{'validation_status': None}, {'validation_status': {'$ne': 'rejected'}}],
            },
            ['ticker', 'score', 'context'],
            sort=[('score', -1)],
            limit=20,
        )
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
        cutoff_news = datetime.now(timezone.utc) - timedelta(hours=12)
        pipeline_news = [
            {"$match": {"ticker": {"$ne": None}, "published_at": {"$gt": cutoff_news}}},
            {"$group": {"_id": "$ticker", "mentions": {"$sum": 1}}},
            {"$match": {"mentions": {"$gte": 3}}},
            {"$sort": {"mentions": -1}},
            {"$limit": 15},
        ]
        news_docs = mongo_store.aggregate("news_articles", pipeline_news)
        for doc in news_docs:
            ticker = doc.get("_id")
            mentions = doc.get("mentions", 0)
            if not ticker:
                continue
            tkr = str(ticker).upper().strip()
            if tkr in existing_set or tkr in FALSE_TICKERS:
                continue
            if tkr not in source_tracker:
                source_tracker[tkr] = {"sources": set(), "mentions": 0}
            source_tracker[tkr]["sources"].add("News")
            source_tracker[tkr]["mentions"] += mentions
        if news_docs:
            logger.info("[DiscoveryMode] News articles: %d trending tickers", len(news_docs))
    except Exception as e:
        logger.warning("[DiscoveryMode] News query failed: %s", e)

    # ── Source 4: Reddit posts (last 12h, 3+ mentions) ──
    try:
        cutoff_reddit = datetime.now(timezone.utc) - timedelta(hours=12)
        pipeline_reddit = [
            {"$match": {"ticker": {"$ne": None}, "created_utc": {"$gt": cutoff_reddit}}},
            {"$group": {"_id": "$ticker", "mentions": {"$sum": 1}}},
            {"$match": {"mentions": {"$gte": 3}}},
            {"$sort": {"mentions": -1}},
            {"$limit": 15},
        ]
        reddit_docs = mongo_store.aggregate("reddit_posts", pipeline_reddit)
        for doc in reddit_docs:
            ticker = doc.get("_id")
            mentions = doc.get("mentions", 0)
            if not ticker:
                continue
            tkr = str(ticker).upper().strip()
            if tkr in existing_set or tkr in FALSE_TICKERS:
                continue
            if tkr not in source_tracker:
                source_tracker[tkr] = {"sources": set(), "mentions": 0}
            source_tracker[tkr]["sources"].add("Reddit")
            source_tracker[tkr]["mentions"] += mentions
        if reddit_docs:
            logger.info("[DiscoveryMode] Reddit posts: %d trending tickers", len(reddit_docs))
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
    from app.utils.us_ticker_resolver import is_us_tradeable

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
        raw_tickers = re.findall(r'\b([A-Z]{1,5})\b', text)

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
