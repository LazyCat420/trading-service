"""
Flash Briefing Generator
Produces short intraday market summaries every 2 hours during market hours.
Summarizes the latest news headlines with source citations.
"""

import logging
import re
from datetime import datetime, timezone, timedelta

from app.services.prism_agent_caller import llm, Priority
from app.services.prism_agent_caller import call_prism_agent
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


async def _get_gainers_losers() -> str:
    """Fetch current daily prices and compute top gainers and losers from watchlist, portfolio, and major tech/index stocks."""
    import asyncio
    import yfinance as yf
    
    watchlist_tickers = []
    portfolio_tickers = []
    try:
        wl_rows = mongo_query.find_rows('watchlist', {'status': 'active'}, ['ticker'])
        watchlist_tickers = [r[0] for r in wl_rows if r[0]]
        pos_rows = mongo_query.find_rows('positions', {}, ['ticker'])
        portfolio_tickers = [r[0] for r in pos_rows if r[0]]
    except Exception as e:
        logger.error(f"[FLASH] Failed to fetch watchlist/portfolio: {e}")

    major_tickers = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "NFLX", "LLY", "JPM", "UNH"]
    all_tickers = list(set(watchlist_tickers + portfolio_tickers + major_tickers))
    
    logger.info(f"[FLASH] Fetching gainers/losers for {len(all_tickers)} tickers...")
    try:
        df = await asyncio.to_thread(
            yf.download, all_tickers, period="2d", group_by="ticker", progress=False, threads=True
        )
        
        results = []
        for ticker in all_tickers:
            try:
                if len(all_tickers) == 1:
                    ticker_df = df.dropna(subset=["Close"])
                else:
                    if ticker not in df.columns.levels[0]:
                        continue
                    ticker_df = df[ticker].dropna(subset=["Close"])
                
                if len(ticker_df) >= 2:
                    prev_close = float(ticker_df["Close"].iloc[-2])
                    curr_price = float(ticker_df["Close"].iloc[-1])
                    if prev_close > 0:
                        pct_change = ((curr_price - prev_close) / prev_close) * 100
                        results.append((ticker, curr_price, pct_change))
                elif len(ticker_df) == 1:
                    # Fallback to fast_info
                    info = yf.Ticker(ticker).fast_info
                    curr_price = info.get("last_price")
                    prev_close = info.get("previous_close")
                    if curr_price and prev_close and prev_close > 0:
                        pct_change = ((curr_price - prev_close) / prev_close) * 100
                        results.append((ticker, float(curr_price), float(pct_change)))
            except Exception:
                continue
                
        if not results:
            return "No gainer/loser data available (yf returned empty)."
            
        results.sort(key=lambda x: x[2], reverse=True)
        
        gainers_str = "\n".join([f"• {t}: ${p:.2f} ({c:+.2f}%)" for t, p, c in results[:5]])
        losers_str = "\n".join([f"• {t}: ${p:.2f} ({c:+.2f}%)" for t, p, c in results[-5:]])
        
        return (
            "### TOP 5 MARKET GAINERS OF THE DAY\n"
            f"{gainers_str}\n\n"
            "### TOP 5 MARKET LOSERS OF THE DAY\n"
            f"{losers_str}\n"
        )
    except Exception as e:
        logger.error(f"[FLASH] Gainer/loser calculation failed: {e}")
        return f"Error loading market gainers/losers: {str(e)}"


async def _get_after_hours_earnings() -> str:
    """Query recent news database for earnings reports from the last 6 hours."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        # ILIKE '%x%' == case-insensitive substring regex.
        patterns = ["earnings", " EPS", "beat", "miss", "revenue", "reports Q"]
        rows = mongo_query.find_rows(
            'news_articles',
            {
                'published_at': {'$gte': cutoff},
                '$or': [
                    {'title': {'$regex': re.escape(p), '$options': 'i'}}
                    for p in patterns
                ],
            },
            ['ticker', 'title', 'summary', 'publisher', 'url', 'published_at'],
            sort=[('published_at', -1)],
            limit=15,
        )

        if not rows:
            return "No after-hours earnings reports detected in news articles from the last 6 hours."

        earnings_text = []
        for r in rows:
            ticker, title, summary, publisher, url, pub_at = r
            ticker_str = f" [{ticker}]" if ticker else ""
            pub_str = pub_at.strftime("%H:%M") if pub_at else ""
            earnings_text.append(
                f"• {title}{ticker_str} — {publisher} ({pub_str})\n"
                f"  Summary: {(summary or '')[:200]}..."
            )
        return "\n".join(earnings_text)
    except Exception as e:
        logger.error(f"[FLASH] Failed to fetch after-hours earnings: {e}")
        return f"Error loading after-hours earnings news: {str(e)}"


async def generate_flash_briefing(report_type: str | None = None) -> str | None:
    """Generate a short flash briefing from the most recently collected news and market data.
    
    Args:
        report_type: 'market_open', 'mid_day', 'market_close_soon', or 'after_hours'.
                     If None, detects automatically based on current Pacific Time.
    """
    import pytz
    pt_tz = pytz.timezone("America/Los_Angeles")
    now_pt = datetime.now(pt_tz)
    
    if report_type is None:
        hour = now_pt.hour
        if 6 <= hour < 9:
            report_type = "market_open"
        elif 9 <= hour < 12:
            report_type = "mid_day"
        elif 12 <= hour < 15:
            report_type = "market_close_soon"
        else:
            report_type = "after_hours"
            
    logger.info(f"[FLASH] Generating flash briefing (type: {report_type})...")

    # Fetch fresh articles before generating briefing
    try:
        from app.collectors.news_collector import collect_all
        logger.info("[FLASH] Fetching fresh articles before generating briefing...")
        await collect_all()
    except ImportError:
        logger.info("[FLASH] news_collector not available, skipping article fetch")

    # Determine database interval and build context
    news_cutoff = datetime.now(timezone.utc) - timedelta(
        hours=8 if report_type == "after_hours" else 4
    )
    # ORDER BY published_at DESC NULLS LAST: Mongo sorts missing/null FIRST on
    # a descending sort, so sort on a presence flag first to push them last.
    _docs = mongo_store.aggregate(
        'news_articles',
        [
            {'$match': {'collected_at': {'$gte': news_cutoff}}},
            {'$addFields': {
                '_no_pub': {'$cond': [
                    {'$eq': [{'$ifNull': ['$published_at', None]}, None]}, 1, 0]},
            }},
            {'$sort': {'_no_pub': 1, 'published_at': -1}},
            {'$limit': 40},
            {'$project': {'_id': 0, 'title': 1, 'publisher': 1, 'url': 1,
                          'ticker': 1, 'published_at': 1}},
        ],
    )
    rows = [
        (d.get('title'), d.get('publisher'), d.get('url'), d.get('ticker'),
         d.get('published_at'))
        for d in _docs
    ]

    # Build context for the LLM
    articles_text = []
    source_urls = []
    for r in rows:
        title, publisher, url, ticker, pub_at = r
        ticker_str = f" [{ticker}]" if ticker else ""
        pub_str = pub_at.strftime("%H:%M") if pub_at else ""
        articles_text.append(f"• {title}{ticker_str} — {publisher} ({pub_str})")
        if url:
            source_urls.append(url)

    news_context = "\n".join(articles_text)

    # Dynamic context based on report type
    if report_type == "after_hours":
        logger.info("[FLASH] Gathering after-hours earnings context...")
        earnings_info = await _get_after_hours_earnings()
        context = (
            f"## AFTER-HOURS EARNINGS\n{earnings_info}\n\n"
            f"## GENERAL MARKET NEWS\n{news_context}"
        )
        system_prompt = (
            "You are an after-hours financial news desk analyst. Given the following headlines and after-hours earnings details, "
            "write a concise 200-300 word after-hours market report. "
            "First, summarize any earnings reports that happened after hours, including the beat/miss details and stock reaction if mentioned. "
            "Then, highlight key focus areas, stocks, or macro events to focus on for the next trading day. "
            "End with a 'Sources' section listing the top 5 most important article URLs. "
            "Output in Markdown format."
        )
    else:
        logger.info("[FLASH] Gathering intraday gainers/losers context...")
        gainers_losers_info = await _get_gainers_losers()
        context = (
            f"{gainers_losers_info}\n\n"
            f"## HEADLINES FOR THE DAY\n{news_context}"
        )
        system_prompt = (
            f"You are a financial news desk analyst. Given the following top gainers/losers of the day and recent headlines, "
            f"write a concise 200-300 word market open/intraday flash briefing (Current Report Type: {report_type.replace('_', ' ').title()}). "
            f"Present the top gainers and losers, highlighting key trends. "
            f"Group related news stories together, focusing on what is affecting the market today. "
            f"Mention specific tickers where relevant. "
            f"End with a 'Sources' section listing the top 5 most important article URLs. "
            f"Output in Markdown format."
        )

    response, tokens, ms = await call_prism_agent(
        agent_id="CUSTOM_V3_JUNIOR_ANALYST",
        user_message=context,
        fallback_system_prompt=system_prompt,
        fallback_agent_name="flash_briefing",
        temperature=0.3,
        max_tokens=8192,
        priority=Priority.NORMAL,
    )

    # Save to DB
    try:
        mongo_store.insert_docs('flash_briefings', [{'report_content': response, 'source_urls': source_urls[:10], 'article_count': len(rows)}])
        logger.info("[FLASH] Saved flash briefing (%d articles summarized)", len(rows))
    except Exception as e:
        logger.error("[FLASH] Failed to save: %s", e)

    return response


def get_recent_flash_briefings(limit: int = 10) -> list[dict]:
    """Fetch the most recent flash briefings."""
    from app.utils.tz import utc_iso
    try:
        rows = mongo_query.find_rows('flash_briefings', {}, ['id', 'created_at', 'report_content', 'source_urls', 'article_count'], sort=[('created_at', -1)], limit=limit)

        return [
            {
                "id": r[0],
                "created_at": utc_iso(r[1]),
                "report_content": r[2],
                "source_urls": r[3] or [],
                "article_count": r[4] or 0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("[FLASH] Failed to fetch flash briefings: %s", e)
        return []
