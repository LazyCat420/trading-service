import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager


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


class RateLimiter:
    """Per-domain async rate limiter.

    Usage:
        async with rate_limiter.acquire("reddit.com"):
            response = await client.get(url)

    Each domain gets its own lock so requests to different domains
    don't block each other. The minimum interval between requests
    to the same domain is 1/rate seconds.
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: dict[str, float] = {}

    @asynccontextmanager
    async def acquire(self, domain: str):
        rate = DOMAIN_LIMITS.get(domain, DEFAULT_RATE)
        min_interval = 1.0 / rate

        async with self._locks[domain]:
            elapsed = time.monotonic() - self._last.get(domain, 0)
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            self._last[domain] = time.monotonic()
            yield


# Singleton instance
rate_limiter = RateLimiter()
