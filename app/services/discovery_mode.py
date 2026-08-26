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


def _symbol_is_known(ticker: str) -> bool:
    """True only if some store has actually seen this symbol.

    The web-search fallback regexes 1-5 letter words out of prose, so "CEO"
    and "IPO" are candidate tickers until proven otherwise. Format checks
    cannot reject them (they are letters-only). Known = present in
    company_registry, ticker_metadata, or a non-rejected discovered_tickers
    row. Fail-closed: a store error keeps the symbol out.
    """
    try:
        if mongo_store.count_docs("company_registry", {"symbol": ticker}):
            return True
        if mongo_store.count_docs("ticker_metadata", {"ticker": ticker}):
            return True
        if mongo_store.count_docs(
            "discovered_tickers",
            {"ticker": ticker, "validation_status": {"$ne": "rejected"}},
        ):
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[DiscoveryMode] symbol existence check failed for %s: %s", ticker, e)
    return False


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

    # ── Sources 3+4: trending mentions, last 12h with a 3+ floor ──
    # Same aggregation the cycle-level discovery sweep runs; one authority
    # in app/services/trend_sources.py (ch.97 consolidation).
    from app.services.trend_sources import trending_mentions

    cutoff_12h = datetime.now(timezone.utc) - timedelta(hours=12)
    for collection, source_label in (
        ("news_articles", "News"),
        ("reddit_posts", "Reddit"),
    ):
        rows_tm = trending_mentions(
            collection, cutoff_12h, limit=15, min_mentions=3,
            context=f"discovery_mode {source_label.lower()}",
        )
        for ticker, mentions in rows_tm:
            tkr = str(ticker).upper().strip()
            if tkr in existing_set or tkr in FALSE_TICKERS:
                continue
            if tkr not in source_tracker:
                source_tracker[tkr] = {"sources": set(), "mentions": 0}
            source_tracker[tkr]["sources"].add(source_label)
            source_tracker[tkr]["mentions"] += mentions
        if rows_tm:
            logger.info("[DiscoveryMode] %s: %d trending tickers", source_label, len(rows_tm))

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
            # Discovery leads bypass the screener: there is no real
            # price/RSI/volume here. The renderer shows n/a instead of the
            # fabricated neutrals the gatekeeper used to be judged on (ch.97).
            "no_market_data": True,
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
            # Every 1-5 letter word in a search result matches the regex, and
            # sources 1-5's US filter cannot help here (it is format-based, and
            # these are all-letters by construction). Now that discovery leads
            # enter `all_pool` — i.e. become genuinely selectable — an invented
            # symbol must be stopped HERE: only symbols some store has actually
            # seen may pass (ch.97).
            if not _symbol_is_known(t):
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
                "no_market_data": True,
            })
        return candidates[:5]
    except Exception as e:
        logger.warning("[DiscoveryMode] Web search extraction failed: %s", e)
        return []
