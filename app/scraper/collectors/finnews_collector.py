"""
finnews_collector.py — Financial News API Collector
----------------------------------------------------
Fetches news articles from 10 free-tier financial news APIs.
Domain-agnostic: no ticker extraction, no DB writes, no trading logic.
Returns normalized FinNewsArticle dataclass objects.

Providers:
  - Marketaux (entity-tagged, ticker-filtered)
  - Finnhub   (company news, highest volume)
  - AlphaVantage (NEWS_SENTIMENT with scores)
  - Polygon/Massive (ticker-linked, publisher quality)
  - NewsAPI   (keyword search)
  - GNews     (keyword search)
  - CurrentsAPI (keyword search, high volume)
  - TheNewsAPI (keyword search, entity detection)
  - WorldNewsAPI (full-text, best for analysis)
  - StockData (entity-tagged, sentiment)

All API keys come from environment variables.
Providers without a key are silently skipped.
"""

import hashlib
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, UTC

import httpx

logger = logging.getLogger(__name__)

# Timeout for all provider requests
_TIMEOUT = 20.0


@dataclass
class FinNewsArticle:
    """Normalized financial news article from any API provider."""

    id: str
    title: str
    url: str
    summary: str
    publisher: str                     # Source name (e.g. "Reuters via Marketaux")
    published_at: datetime | None
    source_type: str = "api"           # Always 'api' for this collector
    provider: str = ""                 # 'marketaux', 'finnhub', etc.
    tickers: list[str] = field(default_factory=list)
    sentiment: float | None = None     # Provider sentiment score if available
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _article_id(title: str, provider: str, url: str) -> str:
    """Deterministic ID from title + provider + url for dedup."""
    raw = f"{title.strip().lower()}_{provider}_{url}"
    return hashlib.md5(raw.encode()).hexdigest()


def _parse_iso(s: str | None) -> datetime:
    """Best-effort ISO 8601 parse, ALWAYS tz-aware (UTC).

    An ISO string without an offset ("2026-07-15T12:28:00") parses to a
    tz-NAIVE datetime; mixing those with the tz-aware ones from other providers
    made fetch_all's `all_articles.sort(key=published_at)` raise "can't compare
    offset-naive and offset-aware datetimes" and zeroed out the combined result.
    Normalise here so every article carries a comparable, tz-aware timestamp.
    """
    if not s:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.now(UTC)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _serialize_article(article: FinNewsArticle) -> dict:
    """Convert FinNewsArticle to JSON-safe dict for API responses."""
    d = asdict(article)
    d["published_at"] = article.published_at.isoformat() if article.published_at else None
    d["first_seen_at"] = article.first_seen_at.isoformat()
    return d


# ---------------------------------------------------------------------------
# Provider Configuration
# ---------------------------------------------------------------------------

_PROVIDER_ENV_KEYS = {
    "finnhub": "FINNHUB_API_KEY",
    "marketaux": "MARKETAUX_API_KEY",
    "newsapi": "NEWSAPI_API_KEY",
    "alphavantage": "ALPHAVANTAGE_API_KEY",
    "polygon": "POLYGON_API_KEY",
    "gnews": "GNEWS_API_KEY",
    "currentsapi": "CURRENTS_API_KEY",
    "thenewsapi": "THENEWSAPI_KEY",
    "worldnewsapi": "WORLDNEWSAPI_KEY",
    "stockdata": "STOCKDATA_API_KEY",
}


def _get_key(provider: str) -> str:
    """Read API key from env. Returns empty string if not set."""
    env_var = _PROVIDER_ENV_KEYS.get(provider, "")
    if not env_var:
        return ""
    key = os.environ.get(env_var, "")
    # Also check MASSIVE_API_KEY as fallback for polygon
    if not key and provider == "polygon":
        key = os.environ.get("MASSIVE_API_KEY", "")
    return key


def get_available_providers() -> list[str]:
    """Return list of providers that have API keys configured."""
    return [name for name in _PROVIDER_ENV_KEYS if _get_key(name)]


# ---------------------------------------------------------------------------
# Individual Provider Fetchers
# ---------------------------------------------------------------------------


async def _fetch_marketaux(
    client: httpx.AsyncClient,
    symbols: list[str],
    limit: int = 10,
) -> list[FinNewsArticle]:
    """Marketaux — entity-tagged financial news."""
    api_key = _get_key("marketaux")
    if not api_key:
        return []

    symbols_str = ",".join(s.upper() for s in symbols[:5])
    url = (
        f"https://api.marketaux.com/v1/news/all"
        f"?symbols={symbols_str}&filter_entities=true"
        f"&language=en&limit={limit}&api_token={api_key}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning("[finnews] marketaux HTTP %d", resp.status_code)
        return []

    articles = []
    for item in resp.json().get("data", []):
        pub = _parse_iso(item.get("published_at"))
        entity_tickers = [
            e["symbol"] for e in item.get("entities", []) if e.get("symbol")
        ]
        sentiment = None
        entities = item.get("entities", [])
        if entities and entities[0].get("sentiment_score") is not None:
            sentiment = entities[0]["sentiment_score"]

        title = item.get("title", "")
        article_url = item.get("url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "marketaux", article_url),
            title=title,
            url=article_url,
            summary=item.get("description", ""),
            publisher=f"{item.get('source', 'unknown')} via Marketaux",
            published_at=pub,
            provider="marketaux",
            tickers=entity_tickers,
            sentiment=sentiment,
        ))
    return articles


async def _fetch_finnhub(
    client: httpx.AsyncClient,
    symbol: str,
    days_back: int = 7,
    limit: int = 15,
) -> list[FinNewsArticle]:
    """Finnhub — company news, highest volume free tier."""
    api_key = _get_key("finnhub")
    if not api_key:
        return []

    from datetime import timedelta
    end = datetime.now(UTC)
    start = end - timedelta(days=days_back)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    url = (
        f"https://finnhub.io/api/v1/company-news"
        f"?symbol={symbol.upper()}&from={start_str}&to={end_str}&token={api_key}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning("[finnews] finnhub HTTP %d", resp.status_code)
        return []

    articles = []
    news = resp.json()
    if not isinstance(news, list):
        return []

    # Sort newest first, cap at limit
    news.sort(key=lambda a: a.get("datetime", 0), reverse=True)

    for item in news[:limit]:
        ts = item.get("datetime", 0)
        pub = datetime.fromtimestamp(ts, tz=UTC) if ts else datetime.now(UTC)
        title = item.get("headline", "")
        article_url = item.get("url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "finnhub", article_url),
            title=title,
            url=article_url,
            summary=item.get("summary", ""),
            publisher=item.get("source", "finnhub"),
            published_at=pub,
            provider="finnhub",
            tickers=[symbol.upper()],
        ))
    return articles


async def _fetch_alphavantage(
    client: httpx.AsyncClient,
    tickers: list[str],
    limit: int = 10,
) -> list[FinNewsArticle]:
    """AlphaVantage NEWS_SENTIMENT — includes per-ticker sentiment scores."""
    api_key = _get_key("alphavantage")
    if not api_key:
        return []

    symbols = ",".join(t.upper() for t in tickers[:5])
    url = (
        f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
        f"&tickers={symbols}&limit={limit}&apikey={api_key}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning("[finnews] alphavantage HTTP %d", resp.status_code)
        return []

    data = resp.json()
    articles = []
    for item in data.get("feed", []):
        pub_str = item.get("time_published", "")
        try:
            pub_dt = datetime.strptime(pub_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pub_dt = datetime.now(UTC)

        ticker_sentiments = item.get("ticker_sentiment", [])
        article_tickers = [ts["ticker"] for ts in ticker_sentiments if ts.get("ticker")]
        overall_sentiment = float(item.get("overall_sentiment_score", 0) or 0)

        title = item.get("title", "")
        article_url = item.get("url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "alphavantage", article_url),
            title=title,
            url=article_url,
            summary=item.get("summary", ""),
            publisher=f"{item.get('source', 'unknown')} via AlphaVantage",
            published_at=pub_dt,
            provider="alphavantage",
            tickers=article_tickers,
            sentiment=overall_sentiment,
        ))
    return articles


async def _fetch_polygon(
    client: httpx.AsyncClient,
    ticker: str,
    limit: int = 10,
) -> list[FinNewsArticle]:
    """Polygon/Massive — ticker-linked articles from premium publishers."""
    api_key = _get_key("polygon")
    if not api_key:
        return []

    url = (
        f"https://api.polygon.io/v2/reference/news"
        f"?ticker={ticker.upper()}&limit={limit}&sort=published_utc"
        f"&order=desc&apiKey={api_key}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning("[finnews] polygon HTTP %d", resp.status_code)
        return []

    articles = []
    for item in resp.json().get("results", []):
        pub = _parse_iso(item.get("published_utc"))
        title = item.get("title", "")
        article_url = item.get("article_url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "polygon", article_url),
            title=title,
            url=article_url,
            summary=item.get("description", ""),
            publisher=f"{item.get('publisher', {}).get('name', 'unknown')} via Polygon",
            published_at=pub,
            provider="polygon",
            tickers=item.get("tickers", []),
        ))
    return articles


async def _fetch_newsapi(
    client: httpx.AsyncClient,
    query: str,
    limit: int = 10,
) -> list[FinNewsArticle]:
    """NewsAPI — keyword search across news sources."""
    api_key = _get_key("newsapi")
    if not api_key:
        return []

    url = (
        f"https://newsapi.org/v2/everything"
        f"?q={query}&language=en&sortBy=publishedAt"
        f"&pageSize={limit}&apiKey={api_key}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning("[finnews] newsapi HTTP %d", resp.status_code)
        return []

    articles = []
    for item in resp.json().get("articles", []):
        pub = _parse_iso(item.get("publishedAt"))
        title = item.get("title", "")
        article_url = item.get("url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "newsapi", article_url),
            title=title,
            url=article_url,
            summary=item.get("description", "") or item.get("content", ""),
            publisher=f"{item.get('source', {}).get('name', 'unknown')} via NewsAPI",
            published_at=pub,
            provider="newsapi",
        ))
    return articles


async def _fetch_gnews(
    client: httpx.AsyncClient,
    query: str,
    limit: int = 10,
) -> list[FinNewsArticle]:
    """GNews — keyword search with country filtering."""
    api_key = _get_key("gnews")
    if not api_key:
        return []

    url = (
        f"https://gnews.io/api/v4/search?q={query}&lang=en&max={limit}&token={api_key}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning("[finnews] gnews HTTP %d", resp.status_code)
        return []

    articles = []
    for item in resp.json().get("articles", []):
        pub = _parse_iso(item.get("publishedAt"))
        title = item.get("title", "")
        article_url = item.get("url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "gnews", article_url),
            title=title,
            url=article_url,
            summary=item.get("description", "") or item.get("content", ""),
            publisher=f"{item.get('source', {}).get('name', 'unknown')} via GNews",
            published_at=pub,
            provider="gnews",
        ))
    return articles


async def _fetch_currentsapi(
    client: httpx.AsyncClient,
    query: str,
    limit: int = 10,
) -> list[FinNewsArticle]:
    """CurrentsAPI — high-volume keyword search."""
    api_key = _get_key("currentsapi")
    if not api_key:
        return []

    url = (
        f"https://api.currentsapi.services/v1/search"
        f"?keywords={query}&language=en&limit={limit}&apiKey={api_key}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning("[finnews] currentsapi HTTP %d", resp.status_code)
        return []

    articles = []
    for item in resp.json().get("news", []):
        pub_str = item.get("published", "")
        pub = _parse_iso(pub_str) if pub_str else datetime.now(UTC)
        title = item.get("title", "")
        article_url = item.get("url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "currentsapi", article_url),
            title=title,
            url=article_url,
            summary=item.get("description", ""),
            publisher=f"CurrentsAPI",
            published_at=pub,
            provider="currentsapi",
        ))
    return articles


async def _fetch_thenewsapi(
    client: httpx.AsyncClient,
    query: str,
    limit: int = 10,
) -> list[FinNewsArticle]:
    """TheNewsAPI — keyword search with entity detection."""
    api_key = _get_key("thenewsapi")
    if not api_key:
        return []

    url = (
        f"https://api.thenewsapi.com/v1/news/all"
        f"?search={query}&language=en&limit={limit}&api_token={api_key}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning("[finnews] thenewsapi HTTP %d", resp.status_code)
        return []

    articles = []
    for item in resp.json().get("data", []):
        pub = _parse_iso(item.get("published_at"))
        title = item.get("title", "")
        article_url = item.get("url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "thenewsapi", article_url),
            title=title,
            url=article_url,
            summary=item.get("description", ""),
            publisher=f"TheNewsAPI",
            published_at=pub,
            provider="thenewsapi",
        ))
    return articles


async def _fetch_worldnewsapi(
    client: httpx.AsyncClient,
    query: str,
    limit: int = 10,
) -> list[FinNewsArticle]:
    """WorldNewsAPI — full-text search, best for deep analysis."""
    api_key = _get_key("worldnewsapi")
    if not api_key:
        return []

    url = (
        f"https://api.worldnewsapi.com/search-news"
        f"?text={query}&language=en&number={limit}"
    )
    resp = await client.get(url, headers={"x-api-key": api_key})
    if resp.status_code != 200:
        logger.warning("[finnews] worldnewsapi HTTP %d", resp.status_code)
        return []

    articles = []
    for item in resp.json().get("news", []):
        pub_str = item.get("publish_date", "")
        pub = _parse_iso(pub_str) if pub_str else datetime.now(UTC)
        title = item.get("title", "")
        article_url = item.get("url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "worldnewsapi", article_url),
            title=title,
            url=article_url,
            summary=item.get("text", "")[:2000],
            publisher=f"WorldNewsAPI",
            published_at=pub,
            provider="worldnewsapi",
        ))
    return articles


async def _fetch_stockdata(
    client: httpx.AsyncClient,
    symbols: list[str],
    limit: int = 10,
) -> list[FinNewsArticle]:
    """StockData — entity-tagged with sentiment."""
    api_key = _get_key("stockdata")
    if not api_key:
        return []

    symbols_str = ",".join(s.upper() for s in symbols[:5])
    url = (
        f"https://api.stockdata.org/v1/news/all"
        f"?symbols={symbols_str}&filter_entities=true"
        f"&language=en&limit={limit}&api_token={api_key}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning("[finnews] stockdata HTTP %d", resp.status_code)
        return []

    articles = []
    for item in resp.json().get("data", []):
        pub = _parse_iso(item.get("published_at"))
        entity_tickers = [
            e["symbol"] for e in item.get("entities", []) if e.get("symbol")
        ]
        title = item.get("title", "")
        article_url = item.get("url", "")
        articles.append(FinNewsArticle(
            id=_article_id(title, "stockdata", article_url),
            title=title,
            url=article_url,
            summary=item.get("description", "") or item.get("snippet", ""),
            publisher=f"StockData",
            published_at=pub,
            provider="stockdata",
            tickers=entity_tickers,
        ))
    return articles


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# Maps provider name -> (fetch_function, needs_tickers, needs_query)
# needs_tickers: uses ticker/symbol list
# needs_query: uses keyword query string
_PROVIDER_DISPATCH = {
    "marketaux":    (_fetch_marketaux,    True,  False),
    "finnhub":      (_fetch_finnhub,      True,  False),
    "alphavantage": (_fetch_alphavantage, True,  False),
    "polygon":      (_fetch_polygon,      True,  False),
    "newsapi":      (_fetch_newsapi,      False, True),
    "gnews":        (_fetch_gnews,        False, True),
    "currentsapi":  (_fetch_currentsapi,  False, True),
    "thenewsapi":   (_fetch_thenewsapi,   False, True),
    "worldnewsapi": (_fetch_worldnewsapi, False, True),
    "stockdata":    (_fetch_stockdata,    True,  False),
}


class FinNewsCollector:
    """Fetch financial news from one or all available API providers.

    Usage:
        collector = FinNewsCollector()
        # Single provider:
        articles = await collector.fetch("finnhub", tickers=["AAPL"], limit=10)
        # All available providers:
        articles = await collector.fetch_all(tickers=["AAPL"], query="AAPL earnings")
    """

    async def fetch(
        self,
        provider: str,
        tickers: list[str] | None = None,
        query: str | None = None,
        limit: int = 10,
        days_back: int = 7,
    ) -> list[FinNewsArticle]:
        """Fetch from a single provider."""
        if provider not in _PROVIDER_DISPATCH:
            logger.warning("[finnews] Unknown provider: %s", provider)
            return []

        key = _get_key(provider)
        if not key:
            logger.info("[finnews] No API key for %s, skipping", provider)
            return []

        fetch_fn, needs_tickers, needs_query = _PROVIDER_DISPATCH[provider]
        tickers = tickers or []
        query = query or " ".join(tickers) or "stock market"

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                if provider == "finnhub" and tickers:
                    # Finnhub is per-symbol
                    all_articles = []
                    for symbol in tickers[:5]:
                        arts = await fetch_fn(client, symbol, days_back=days_back, limit=limit)
                        all_articles.extend(arts)
                    return all_articles
                elif needs_tickers and tickers:
                    if provider in ("polygon",):
                        # Polygon is single-ticker
                        return await fetch_fn(client, tickers[0], limit=limit)
                    else:
                        return await fetch_fn(client, tickers, limit=limit)
                elif needs_query:
                    return await fetch_fn(client, query, limit=limit)
                else:
                    # Ticker-based provider but no tickers given — use query fallback
                    logger.info("[finnews] %s needs tickers but none provided, skipping", provider)
                    return []
        except Exception as e:
            logger.error("[finnews] %s fetch error: %s", provider, e)
            return []

    async def fetch_all(
        self,
        tickers: list[str] | None = None,
        query: str | None = None,
        limit: int = 10,
        days_back: int = 7,
    ) -> list[FinNewsArticle]:
        """Fetch from all providers with available API keys."""
        available = get_available_providers()
        if not available:
            logger.warning("[finnews] No API keys configured for any provider")
            return []

        logger.info("[finnews] Fetching from %d providers: %s", len(available), available)

        all_articles: list[FinNewsArticle] = []
        seen_urls: set[str] = set()

        for provider in available:
            try:
                articles = await self.fetch(
                    provider,
                    tickers=tickers,
                    query=query,
                    limit=limit,
                    days_back=days_back,
                )
                for a in articles:
                    if a.url and a.url not in seen_urls:
                        seen_urls.add(a.url)
                        all_articles.append(a)
                if articles:
                    logger.info("[finnews] %s: %d articles", provider, len(articles))
            except Exception as e:
                logger.warning("[finnews] %s failed: %s", provider, e)

        # Sort newest first
        all_articles.sort(
            key=lambda x: x.published_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

        logger.info(
            "[finnews] Total: %d unique articles from %d providers",
            len(all_articles), len(available),
        )
        return all_articles
