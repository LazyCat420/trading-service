"""
API Rate Limiter — shared per-service semaphores for safe parallel collection.

When multiple tickers collect data in parallel, each ticker fires off requests
to yfinance, finnhub, Reddit, YouTube, etc. Without rate limiting, 5 tickers ×
5 sources = 25 concurrent HTTP requests, which can trigger IP bans and API
rate limits.

The semaphore machinery moved to the SDK (lazycat.ratelimit.KeyedSemaphore);
the per-service limits stay here because they come from this app's settings.

Usage is unchanged:
    from app.services.api_rate_limiter import rate_limiter

    async with rate_limiter.acquire("yfinance"):
        await collect_price_history(ticker)

    @rate_limiter.limit("reddit")
    async def collect_reddit_posts(ticker):
        ...
"""

import logging

from lazycat.ratelimit import KeyedSemaphore

from app.config import settings

logger = logging.getLogger(__name__)

# Max concurrent requests per external service.
SERVICE_LIMITS: dict[str, int] = {
    "yfinance": settings.YFINANCE_MAX_CONCURRENT,
    "finnhub": settings.FINNHUB_MAX_CONCURRENT,
    "reddit": settings.REDDIT_MAX_CONCURRENT,
    "youtube": settings.YOUTUBE_MAX_CONCURRENT,
    "yf_news": settings.YFINANCE_MAX_CONCURRENT,  # shares yfinance limit
}

# Singleton — import this everywhere. Unknown services get 3 concurrent slots,
# matching the previous default.
rate_limiter = KeyedSemaphore(limits=SERVICE_LIMITS, default_limit=3)

__all__ = ["rate_limiter", "SERVICE_LIMITS"]
