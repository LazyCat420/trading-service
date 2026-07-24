"""
Twitter Collector — Fetches tweets from whitelisted accounts and searches cashtags via scraper-service.
---------------------------------------------------------------------------------------------
Applies deduplication using DedupEngine and stores results in the social_posts table.
"""

import logging
import hashlib
import json
import asyncio
from datetime import datetime, timezone, timedelta
from app.db.connection import get_db
from app.processors.dedup_engine import DedupEngine
from app.processors.ticker_extractor import get_ticker_symbols
from app.services.scraper_client import scraper_client

logger = logging.getLogger(__name__)

# FinTwit accounts to monitor (high-signal, widely followed)
FINTWIT_ACCOUNTS = [
    "unusual_whales", "DeItaone", "Fxhedgers", "zaborsky_daniel",
    "jimcramer", "elonmusk", "chaikinapps", "realwillmeade",
    "hedgeye", "tradingview", "LiveSquawk", "FirstSquawk",
]

CRYPTO_ACCOUNTS = [
    "whale_alert", "lookonchain", "EmberCN", "WuBlockchain",
]

def _is_quality_tweet(tweet: dict) -> bool:
    """Filter out retweets and extremely low engagement tweets."""
    if tweet.get("is_retweet"):
        return False
    
    # Check likes/views to ensure some minimal engagement
    likes = tweet.get("like_count", 0)
    if likes < 5:
        return False
        
    return True

async def _store_tweets(tweets: list[dict], default_ticker: str | None = None) -> int:
    """Process, deduplicate, extract tickers, and store tweets in the database."""
    if not tweets:
        return 0
        
    dedup = DedupEngine(table="social_posts")
    stored_count = 0
    
    # We will query and insert in a loop or batch. Let's do it cleanly
    for t in tweets:
        if not _is_quality_tweet(t):
            continue
            
        content = t.get("text", "")
        # Extract tickers mentioned
        tickers_found = set(await get_ticker_symbols(content))
        
        # If a default ticker was specified, make sure it is in the list
        if default_ticker:
            tickers_found.add(default_ticker.upper())
            
        if not tickers_found:
            # If no ticker found and not from a whitelist account, skip
            # (general Fintwit sweep may store with ticker=None or general market signal)
            tickers_found = {None}
            
        for ticker in tickers_found:
            # Check duplicate via Jaccard/exact match
            if dedup.is_duplicate(content):
                continue
                
            # Compute hash of content for database content_hash column
            content_hash = dedup.compute_hash(content)
            
            # Compute unique primary key: sha256(platform + post_id + ticker)
            raw_id = f"twitter_{t['id']}_{ticker or 'market'}"
            db_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()
            
            # Parse timestamp
            posted_at = None
            if t.get("created_at"):
                try:
                    posted_at = datetime.fromisoformat(t["created_at"])
                except Exception:
                    pass
            
            # Convert tags lists to JSON strings for DB insertion
            cashtags_json = json.dumps(t.get("cashtags", []))
            hashtags_json = json.dumps(t.get("hashtags", []))
            
            # Insert into database
            try:
                with get_db() as db:
                    db.execute("""
                        INSERT INTO social_posts (
                            id, platform, platform_post_id, ticker, author_username,
                            author_display_name, author_followers, content, like_count,
                            repost_count, reply_count, view_count, cashtags, hashtags,
                            is_repost, posted_at, content_hash, collected_at
                        ) VALUES (
                            %s, 'twitter', %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, CURRENT_TIMESTAMP
                        ) ON CONFLICT (id) DO NOTHING
                    """, [
                        db_id, t["id"], ticker, t["author_username"],
                        t["author_display_name"], t["author_followers"], content, t["like_count"],
                        t["retweet_count"], t["reply_count"], t["view_count"], cashtags_json, hashtags_json,
                        t["is_retweet"], posted_at, content_hash
                    ])
                    stored_count += 1
            except Exception as e:
                logger.error(f"Failed to insert tweet {t['id']} to DB: {e}")
                
    return stored_count

async def collect_for_ticker(ticker: str, limit: int = 50) -> int:
    """Search Twitter for a ticker cashtag and company name."""
    logger.info(f"Collecting Twitter data for ticker: {ticker}")
    try:
        items = await scraper_client.collect(
            source="twitter",
            req_data={
                "cashtags": [ticker.upper()],
                "limit": limit
            }
        )
        count = await _store_tweets(items, default_ticker=ticker)
        logger.info(f"Stored {count} tweets for ticker: {ticker}")
        return count
    except Exception as e:
        logger.error(f"Twitter collection failed for ticker {ticker}: {e}")
        return 0

async def collect_fintwit_sweep(limit: int = 20) -> int:
    """Sweep all whitelisted FinTwit and Crypto accounts for latest tweets."""
    logger.info("Starting Twitter FinTwit and Crypto accounts sweep")
    all_accounts = FINTWIT_ACCOUNTS + CRYPTO_ACCOUNTS
    total_stored = 0
    
    # Scrape user feeds in batches to avoid overwhelming the scraper-service
    batch_size = 3
    for i in range(0, len(all_accounts), batch_size):
        batch = all_accounts[i:i+batch_size]
        logger.debug(f"Twitter sweep batch: {batch}")
        
        try:
            items = await scraper_client.collect(
                source="twitter",
                req_data={
                    "usernames": batch,
                    "limit": limit
                }
            )
            count = await _store_tweets(items)
            total_stored += count
        except Exception as e:
            logger.error(f"Twitter sweep failed for batch {batch}: {e}")
            
        await asyncio.sleep(2)  # brief pause between batches
        
    if total_stored == 0:
        # A full sweep of 16 high-volume accounts yielding literally nothing
        # means the backend is broken, not that FinTwit went quiet — as of
        # 07-24 the scraper-service has no TWITTER_ACCOUNTS credentials
        # configured, so twscrape returns [] for every request.
        logger.warning(
            "Twitter accounts sweep stored 0 tweets across %d accounts — "
            "scraper backend likely unconfigured (TWITTER_ACCOUNTS env in "
            "scraper-service).", len(all_accounts))
    else:
        logger.info(f"Twitter accounts sweep complete. Stored {total_stored} tweets.")
    return total_stored

async def collect_all() -> int:
    """Run general sweep: watchlist tickers cashtags + whitelist account feeds."""
    # Fetch watchlist tickers from DB
    tickers = []
    try:
        with get_db() as db:
            db.execute("SELECT ticker FROM watchlist WHERE status = 'active'")
            tickers = [r[0] for r in db.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch watchlist tickers: {e}")
        
    total = 0
    # 1. Sweep whitelist accounts
    total += await collect_fintwit_sweep()
    
    # 2. Search cashtags for watchlist tickers
    for ticker in tickers:
        count = await collect_for_ticker(ticker, limit=20)
        total += count
        await asyncio.sleep(1)
        
    return total
