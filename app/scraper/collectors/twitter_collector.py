"""
twitter_collector.py — Domain-agnostic Twitter/X post collection using twscrape.
-----------------------------------------------------------------------------
All trading-specific logic (ticker extraction, DB writes) resides in the caller
(trading-service). This service only handles raw Twitter scraping.
"""

import json
import os
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from twscrape import API

logger = logging.getLogger(__name__)

@dataclass
class Tweet:
    id: str
    text: str
    author_username: str
    author_display_name: str
    author_followers: int
    created_at: datetime
    retweet_count: int
    like_count: int
    reply_count: int
    quote_count: int
    view_count: int
    lang: str
    cashtags: list[str]
    hashtags: list[str]
    urls: list[str]
    is_retweet: bool
    is_quote: bool
    quoted_tweet_id: str | None

def _serialize_tweet(t: Tweet) -> dict:
    """Serialize Tweet dataclass to dict."""
    return {
        "id": t.id,
        "text": t.text,
        "author_username": t.author_username,
        "author_display_name": t.author_display_name,
        "author_followers": t.author_followers,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "retweet_count": t.retweet_count,
        "like_count": t.like_count,
        "reply_count": t.reply_count,
        "quote_count": t.quote_count,
        "view_count": t.view_count,
        "lang": t.lang,
        "cashtags": t.cashtags,
        "hashtags": t.hashtags,
        "urls": t.urls,
        "is_retweet": t.is_retweet,
        "is_quote": t.is_quote,
        "quoted_tweet_id": t.quoted_tweet_id,
    }

class TwitterCollector:
    def __init__(self):
        self._api = None

    async def get_api(self) -> API:
        """Lazily initialize API and pool accounts from environment."""
        if self._api is not None:
            return self._api

        # Set DB path to a stable location
        db_path = os.getenv("TWSCRAPE_DB", os.path.expanduser("~/.twscrape/accounts.db"))
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # Initialize twscrape API
        self._api = API(db_path)
        
        accounts_str = os.getenv("TWITTER_ACCOUNTS", "[]")
        try:
            accounts = json.loads(accounts_str)
        except Exception as e:
            logger.warning(f"Failed to parse TWITTER_ACCOUNTS: {e}")
            accounts = []

        if not accounts:
            logger.warning("No accounts found in TWITTER_ACCOUNTS env var. twscrape calls will likely fail.")
            
        for acc in accounts:
            username = acc.get("username")
            password = acc.get("password")
            email = acc.get("email")
            email_password = acc.get("email_password")
            
            if username and password:
                try:
                    # add_account is safe if account already exists
                    await self._api.pool.add_account(username, password, email, email_password)
                    logger.info(f"Added Twitter account to pool: {username}")
                except Exception as e:
                    logger.error(f"Failed to add Twitter account {username} to pool: {e}")
                    
        # Log in accounts that are inactive/unauthenticated
        try:
            await self._api.pool.login_all()
        except Exception as e:
            logger.warning(f"Error logging in accounts: {e}")
            
        return self._api

    def _convert_tweet(self, t) -> Tweet:
        """Convert twscrape Tweet model to our internal Tweet dataclass."""
        urls = [link.url for link in t.links if getattr(link, "url", None)] if getattr(t, "links", None) else []
        
        # Handle retweet / quote tweet check
        is_retweet = t.retweetedTweet is not None if getattr(t, "retweetedTweet", None) is not None else False
        is_quote = t.quotedTweet is not None if getattr(t, "quotedTweet", None) is not None else False
        quoted_tweet_id = str(t.quotedTweet.id) if is_quote and getattr(t.quotedTweet, "id", None) else None
        
        # Get user details
        user_username = t.user.username if t.user else "unknown"
        user_display = t.user.displayname if t.user else "unknown"
        user_followers = t.user.followersCount if t.user else 0
        
        return Tweet(
            id=str(t.id),
            text=t.rawContent if getattr(t, "rawContent", None) else "",
            author_username=user_username,
            author_display_name=user_display,
            author_followers=user_followers,
            created_at=t.date if getattr(t, "date", None) else datetime.now(timezone.utc),
            retweet_count=t.retweetCount if getattr(t, "retweetCount", None) is not None else 0,
            like_count=t.likeCount if getattr(t, "likeCount", None) is not None else 0,
            reply_count=t.replyCount if getattr(t, "replyCount", None) is not None else 0,
            quote_count=t.quoteCount if getattr(t, "quoteCount", None) is not None else 0,
            view_count=t.viewCount if getattr(t, "viewCount", None) is not None else 0,
            lang=t.lang if getattr(t, "lang", None) else "en",
            cashtags=t.cashtags if getattr(t, "cashtags", None) else [],
            hashtags=t.hashtags if getattr(t, "hashtags", None) else [],
            urls=urls,
            is_retweet=is_retweet,
            is_quote=is_quote,
            quoted_tweet_id=quoted_tweet_id,
        )

    async def search(self, query: str, limit: int = 50) -> list[Tweet]:
        """Search for tweets matching a query."""
        api = await self.get_api()
        tweets = []
        try:
            async for tweet in api.search(query, limit=limit):
                # Apply high-level general filters (e.g. skip pure retweets or extremely low engagement if desired)
                # Let's keep domain-agnostic: return what we get, let caller apply stricter logic
                tweets.append(self._convert_tweet(tweet))
        except Exception as e:
            logger.error(f"Twitter search failed for query '{query}': {e}")
        return tweets

    async def get_user_tweets(self, username: str, limit: int = 50) -> list[Tweet]:
        """Fetch latest tweets from a specific user."""
        api = await self.get_api()
        tweets = []
        try:
            user = await api.user_by_username(username)
            if not user:
                logger.warning(f"Could not find Twitter user: {username}")
                return []
            
            async for tweet in api.user_tweets(user.id, limit=limit):
                tweets.append(self._convert_tweet(tweet))
        except Exception as e:
            logger.error(f"Failed to fetch tweets for user '{username}': {e}")
        return tweets

    async def get_cashtag_feed(self, cashtag: str, limit: int = 50) -> list[Tweet]:
        """Fetch latest tweets for a specific cashtag (e.g., $AAPL)."""
        # Search query for cashtag is simply "$TICKER"
        query = f"${cashtag.upper()}"
        return await self.search(query, limit=limit)
