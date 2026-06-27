import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.db.connection import get_db
from app.utils.text_utils import format_db_section

logger = logging.getLogger(__name__)

async def build_ticker_data_report(ticker: str) -> str:
    """Collect core stock datasets in parallel and format them into a markdown report."""
    ticker = ticker.upper().strip()
    
    # 1. Run Collectors in Parallel
    from app.collectors.yfinance_collector import collect_price_history, collect_fundamentals
    from app.collectors.news_collector import collect_finnhub_news
    from app.collectors.reddit_collector import collect_for_ticker as collect_reddit
    from app.collectors.youtube_collector import collect_for_ticker as collect_youtube
    
    tasks = [
        collect_price_history(ticker, period="6mo"),
        collect_fundamentals(ticker),
        collect_finnhub_news(ticker),
        collect_reddit(ticker),
        collect_youtube(ticker)
    ]
    
    # Execute all collection tasks in parallel (timeout to prevent hanging)
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=45.0)
    except asyncio.TimeoutError:
        logger.warning(f"[V3] Pre-collection for {ticker} timed out after 45s.")
        
    # 2. Fetch Formatted Markdown via existing tools
    from app.tools.finance_tools import get_market_data, get_finnhub_news, get_technical_indicators
    
    market_data_md = await get_market_data(ticker)
    news_md = await get_finnhub_news(ticker)
    tech_md = await get_technical_indicators(ticker)
    
    # 3. Query Database for Reddit & YouTube Markdown directly
    reddit_md = "No recent Reddit sentiment found."
    youtube_md = "No recent YouTube transcripts found."
    
    with get_db() as db:
        # Reddit formatting
        reddit_rows = db.execute(
            """
            SELECT subreddit, title, score, upvote_ratio, comment_count, sentiment_score, summary
            FROM reddit_posts 
            WHERE ticker = %s 
            ORDER BY score DESC LIMIT 10
            """,
            [ticker]
        ).fetchall()
        if reddit_rows:
            reddit_md = format_db_section(
                "Top Reddit Posts", 
                reddit_rows, 
                ["Subreddit", "Title", "Score", "UpvoteRatio", "Comments", "Sentiment", "Summary"]
            )
            
        # YouTube formatting
        yt_rows = db.execute(
            """
            SELECT channel, title, published_at, summary
            FROM youtube_transcripts
            WHERE ticker = %s
            ORDER BY published_at DESC LIMIT 5
            """,
            [ticker]
        ).fetchall()
        if yt_rows:
            youtube_md = format_db_section(
                "Recent YouTube Analyses",
                yt_rows,
                ["Channel", "Title", "Published", "Summary"]
            )

    # 4. Construct Final Document
    report = (
        f"# Pre-Collected Ticker Data Report: {ticker}\n"
        f"Generated at: {datetime.now(timezone.utc).isoformat()}\n\n"
        f"## 1. Market Data & Fundamentals\n"
        f"{market_data_md}\n\n"
        f"## 2. Technical Indicators\n"
        f"{tech_md}\n\n"
        f"## 3. Recent News & Sentiment\n"
        f"{news_md}\n\n"
        f"## 4. Reddit Social Sentiment\n"
        f"{reddit_md}\n\n"
        f"## 5. YouTube Mentions & Transcripts\n"
        f"{youtube_md}\n"
    )
    
    return report
