"""
Per-domain scraper rate limiting.

The limiter mechanism moved to the SDK (lazycat.ratelimit.KeyedRateLimiter);
the domain budget table stays here because it is this app's operational
knowledge, not something to bake into a shared library.

Usage is unchanged:
    from app.scraper.core.rate_limiter import rate_limiter

    async with rate_limiter.acquire("reddit.com"):
        response = await client.get(url)
"""

from lazycat.ratelimit import KeyedRateLimiter

# Requests per second per domain — tune these as you learn each site's limits
DOMAIN_LIMITS: dict[str, float] = {
    "reddit.com": 0.5,              # 1 req per 2 seconds
    "www.reddit.com": 0.5,
    "youtube.com": 1.0,
    "www.youtube.com": 1.0,
    "seekingalpha.com": 0.3,
    "investing.com": 0.3,
    "pubmed.ncbi.nlm.nih.gov": 0.5,
    "feeds.marketwatch.com": 1.0,
    "search.cnbc.com": 1.0,
    "twitter.com": 0.2,
    "x.com": 0.2,
    "api.llama.fi": 1.0,
    "api.stlouisfed.org": 0.5,
    "api.worldbank.org": 1.0,
    "openinsider.com": 0.3,
    # Financial News APIs
    "finnhub.io": 1.0,                  # 60 calls/min free tier
    "api.marketaux.com": 0.5,
    "newsapi.org": 0.5,
    "www.alphavantage.co": 0.2,         # Very low free-tier limit
    "api.polygon.io": 0.08,             # 5 req/min free tier
    "gnews.io": 0.5,
    "api.currentsapi.services": 1.0,
    "api.thenewsapi.com": 0.5,
    "api.worldnewsapi.com": 0.5,
    "api.stockdata.org": 0.5,
}
DEFAULT_RATE = 1.0  # 1 req/s for unknown domains

# Singleton instance. DOMAIN_LIMITS is held by reference, so edits to the table
# above take effect on the next acquire() without rebuilding the limiter.
rate_limiter = KeyedRateLimiter(rates=DOMAIN_LIMITS, default_rate=DEFAULT_RATE)

__all__ = ["rate_limiter", "DOMAIN_LIMITS", "DEFAULT_RATE"]
