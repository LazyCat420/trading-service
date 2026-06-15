"""
stocktwits_collector.py — Domain-aware StockTwits collector.
Writes to social_posts table.
"""
import logging
import datetime
import hashlib
from app.services.scraper_client import scraper_client
from app.processors.dedup_engine import DedupEngine
from app.db.connection import get_db

logger = logging.getLogger(__name__)

async def collect_for_ticker(ticker: str, limit: int = 30) -> int:
    """Fetch StockTwits stream for a ticker and save to social_posts."""
    items = await scraper_client.collect(
        source="stocktwits",
        req_data={"symbol": ticker, "limit": limit}
    )
    
    if not items:
        return 0

    dedup = DedupEngine(table="social_posts", ticker=ticker)
    
    rows = []
    for item in items:
        post_id = item.get("id")
        body = item.get("body", "")
        username = item.get("username", "")
        display_name = item.get("display_name", "")
        followers = item.get("followers", 0)
        sentiment = item.get("sentiment")
        created_at_str = item.get("created_at")

        is_dup = dedup.is_duplicate(body)
        if is_dup:
            continue

        content_hash = dedup.compute_hash(body)
        
        # Parse posted_at datetime
        try:
            posted_at = datetime.datetime.fromisoformat(created_at_str)
        except Exception:
            posted_at = datetime.datetime.now(datetime.timezone.utc)

        # Unique ID constraint to avoid duplicates
        unique_id = hashlib.sha256(f"stocktwits_{post_id}_{ticker}".encode("utf-8")).hexdigest()

        # columns matching migrations: id, platform, platform_post_id, ticker, author_username,
        # author_display_name, author_followers, content, like_count, repost_count, reply_count,
        # view_count, cashtags, hashtags, sentiment_score, quality_score, quality_status,
        # is_repost, posted_at, collected_at, content_hash
        rows.append((
            unique_id, "stocktwits", post_id, ticker, username, display_name,
            followers, body, 0, 0, 0, 0,
            None, None, None, None, "pending", False,
            posted_at, datetime.datetime.now(datetime.timezone.utc), content_hash
        ))

    if rows:
        with get_db() as db:
            db.executemany("""
                INSERT INTO social_posts
                (id, platform, platform_post_id, ticker, author_username, author_display_name,
                 author_followers, content, like_count, repost_count, reply_count, view_count,
                 cashtags, hashtags, sentiment_score, quality_score, quality_status, is_repost,
                 posted_at, collected_at, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, rows)
        logger.info(f"[stocktwits] Wrote {len(rows)} posts for ${ticker}")
        return len(rows)
    return 0
