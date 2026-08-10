"""
News API Rotator — Cycles across free news API providers + RSS feeds.

Rotates through all configured providers so no single source's rate limit
blocks the pipeline. Providers without an API key are automatically skipped.

Integrates with the existing collectors pattern:
  - Uses SmartClient (app/services/request_utils.py) for HTTP with backoff
  - Uses %s placeholders for DB queries (psycopg compat shim)
  - Tags all articles with tickers via _detect_tickers_in_text()
  - Deduplicates via ON CONFLICT (id) DO NOTHING
  - Delegates to real finnhub_collector.collect_news() (not a fake class)

Install deps (already in requirements.txt):
    feedparser>=6.0.12
    httpx>=0.28.0
    markdownify>=0.13.0
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


from app.config import settings
from app.db.connection import get_db
from app.services.request_utils import SmartClient
from app.utils.text_utils import is_truncated_content

logger = logging.getLogger(__name__)

# ── Body upgrade (see _upgrade_bodies) ──────────────────────────────────────
# The bar an article has to clear to be worth storing as one. Shared with the
# scraper's own thin-content gate so the trigger and the gate cannot drift
# apart: asking for a body the scraper would then refuse is pure waste.
try:
    from app.scraper.core.content_quality import MIN_ARTICLE_CHARS as MIN_BODY_CHARS
except Exception:  # noqa: BLE001 — keep the collector importable standalone
    MIN_BODY_CHARS = 900

# Caps, because a run carries up to 10 providers x 10 articles and every
# upgrade is a network fetch. Tunable without a deploy.
#
# Sized to cover a whole run rather than a fraction of it. Measured on the
# deployed container: a SINGLE-ticker call produced **56** candidates — the
# providers are queried on a general query, not per ticker — so a cap of 25
# left 31 articles on their blurb every time. And because the shortest
# summaries go first, what got skipped was exactly the 400-500 char band that
# is the bulk of the problem (alphavantage 0/41 and polygon 0/23 upgraded in
# the first live run).
UPGRADE_LIMIT = int(os.getenv("NEWS_BODY_UPGRADE_LIMIT", "60"))
UPGRADE_CONCURRENCY = int(os.getenv("NEWS_BODY_UPGRADE_CONCURRENCY", "4"))
UPGRADE_BUDGET_S = float(os.getenv("NEWS_BODY_UPGRADE_BUDGET_S", "45"))

# `collect_from_all_apis([ticker])` runs ONCE PER TICKER (data_report.py:274),
# and because the providers are queried generically each ticker sees largely
# the SAME articles. Without a memory, an 80-ticker cycle re-scrapes the same
# ~56 URLs 80 times — measured at 23.0s per ticker. This drops the cost to
# near zero after the first ticker of a cycle.
#
# Successes only, in-process, small and short-lived: bodies are large, and a
# stale body is worse than a re-fetch. Dead URLs are already remembered by the
# scraper's own failure cache, so this is the other half of the same idea.
_BODY_CACHE_TTL_S = float(os.getenv("NEWS_BODY_CACHE_TTL_S", "3600"))
_BODY_CACHE_MAX = int(os.getenv("NEWS_BODY_CACHE_MAX", "512"))
_BODY_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()


def _body_cache_get(url: str) -> str | None:
    hit = _BODY_CACHE.get(url)
    if hit is None:
        return None
    expires_at, body = hit
    if time.time() >= expires_at:
        del _BODY_CACHE[url]
        return None
    _BODY_CACHE.move_to_end(url)
    return body


def _body_cache_put(url: str, body: str) -> None:
    _BODY_CACHE[url] = (time.time() + _BODY_CACHE_TTL_S, body)
    _BODY_CACHE.move_to_end(url)
    while len(_BODY_CACHE) > _BODY_CACHE_MAX:
        _BODY_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class NewsArticle:
    """In-memory article representation before DB write."""

    title: str
    url: str
    summary: str
    source: str
    published_at: datetime
    tickers: list[str] = field(default_factory=list)
    full_text_md: str = ""
    sentiment: float | None = None  # -1.0 to 1.0 if provider supplies it


# ---------------------------------------------------------------------------
# Rate-limit tracker  (in-memory, resets on restart — fine for testing)
# ---------------------------------------------------------------------------


class QuotaTracker:
    """Simple per-provider quota tracker. Thread-safe via asyncio lock."""

    def __init__(self, daily_limit: int, per_minute_limit: int = 999):
        self.daily_limit = daily_limit
        self.per_minute_limit = per_minute_limit
        self._daily_used = 0
        self._minute_used = 0
        self._minute_reset = time.monotonic() + 60
        self._lock = asyncio.Lock()

    async def can_use(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            if now > self._minute_reset:
                self._minute_used = 0
                self._minute_reset = now + 60
            return (
                self._daily_used < self.daily_limit
                and self._minute_used < self.per_minute_limit
            )

    async def consume(self) -> None:
        async with self._lock:
            self._daily_used += 1
            self._minute_used += 1

    def reset_daily(self) -> None:
        self._daily_used = 0


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    name: str
    api_key: str
    daily_limit: int
    per_minute_limit: int = 999
    enabled: bool = True


def build_providers_from_settings() -> list[ProviderConfig]:
    """Build the provider list from app/config.py settings.

    Providers whose API key is empty are marked enabled=False and will
    be silently skipped by the rotator.
    """
    providers = [
        ProviderConfig(
            "finnhub", settings.FINNHUB_API_KEY, daily_limit=800, per_minute_limit=60
        ),
        ProviderConfig("marketaux", settings.MARKETAUX_API_KEY, daily_limit=100),
        ProviderConfig("newsapi", settings.NEWSAPI_API_KEY, daily_limit=100),
        ProviderConfig("alphavantage", settings.ALPHAVANTAGE_API_KEY, daily_limit=25),
        ProviderConfig(
            "polygon",
            settings.POLYGON_API_KEY or settings.MASSIVE_API_KEY,
            daily_limit=999,
            per_minute_limit=5,
        ),
        ProviderConfig("gnews", settings.GNEWS_API_KEY, daily_limit=100),
        ProviderConfig("currentsapi", settings.CURRENTS_API_KEY, daily_limit=600),
        ProviderConfig("thenewsapi", settings.THENEWSAPI_KEY, daily_limit=150),
        ProviderConfig("worldnewsapi", settings.WORLDNEWSAPI_KEY, daily_limit=300),
        ProviderConfig("stockdata", settings.STOCKDATA_API_KEY, daily_limit=100),
    ]
    # Auto-disable providers with no key
    for p in providers:
        if not p.api_key:
            p.enabled = False
    return providers


# ---------------------------------------------------------------------------
# Individual provider fetchers were moved to scraper-service/finnews_collector.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DB persistence — writes NewsArticle objects to the news_articles table
# ---------------------------------------------------------------------------


async def _upgrade_bodies(articles: list[NewsArticle]) -> dict[str, str]:
    """Fetch real article bodies for the provider blurbs that need one.

    The body scrape used to fire only when the API summary was under **150**
    chars. Providers return 300-500, so it never fired — while an article
    needs ~900 to be worth reading. Measured over 30 days:

        source          rows   under 900   avg chars
        finnhub       11,102        98%          303
        alphavantage   4,658        99%          471
        polygon        3,060       100%          457

    and scraping a sample of those blurbs upgraded **17 of 24 (70%)** into
    real articles — 494 -> 6,058 chars, 482 -> 12,458. So ~13k articles a
    month were stored as headlines because nothing asked for the body.

    Raising the trigger alone would have been a bad trade. `_persist_articles`
    awaited each scrape **serially, inside an open DB transaction**, and a run
    carries up to 10 providers x 10 articles: ~100 sequential fetches per
    ticker, with a database connection pinned for the duration. So the
    upgrades happen here instead — before the DB is touched, bounded three
    ways:

      * a cap on how many articles are attempted (shortest summary first,
        since that is where the most is gained),
      * a concurrency limit, so the per-domain rate limiter is respected
        rather than fought,
      * a wall-clock budget, after which whatever finished is used.

    Whatever the caps drop is logged. A cap that is not reported reads as
    "we covered everything".
    """
    from app.collectors.news_collector import _scrape_article_body_via_service

    if not UPGRADE_LIMIT:
        return {}

    # One attempt per URL even when several articles share it.
    candidates: dict[str, int] = {}
    bodies: dict[str, str] = {}
    cached = 0
    for a in articles:
        s = a.summary or ""
        if not a.url or (len(s) >= MIN_BODY_CHARS and "..." not in s):
            continue
        # Already fetched for an earlier ticker this cycle.
        hit = _body_cache_get(a.url)
        if hit is not None:
            bodies[a.url] = hit
            cached += 1
            continue
        candidates[a.url] = min(candidates.get(a.url, len(s)), len(s))
    if cached:
        logger.info("[rotator] body upgrade: %d served from cache", cached)
    if not candidates:
        return bodies

    # Shortest summary first — the blurbs with the least in them already.
    ordered = sorted(candidates, key=lambda u: candidates[u])
    attempted, dropped = ordered[:UPGRADE_LIMIT], ordered[UPGRADE_LIMIT:]
    if dropped:
        logger.info(
            "[rotator] body upgrade: attempting %d of %d candidates "
            "(cap=%d); %d left with their provider blurb",
            len(attempted), len(candidates), UPGRADE_LIMIT, len(dropped),
        )

    sem = asyncio.Semaphore(UPGRADE_CONCURRENCY)

    async def _one(url: str) -> None:
        async with sem:
            try:
                body = await _scrape_article_body_via_service(url)
            except Exception as e:  # noqa: BLE001 — one URL must not stop the run
                logger.debug("[rotator] body upgrade failed for %s: %s", url, e)
                return
            if body and len(body) >= MIN_BODY_CHARS:
                bodies[url] = body
                _body_cache_put(url, body)

    try:
        await asyncio.wait_for(
            asyncio.gather(*(_one(u) for u in attempted), return_exceptions=True),
            timeout=UPGRADE_BUDGET_S,
        )
    except asyncio.TimeoutError:
        logger.info(
            "[rotator] body upgrade hit its %.0fs budget — %d of %d upgraded, "
            "the rest keep their provider blurb",
            UPGRADE_BUDGET_S, len(bodies), len(attempted),
        )

    if bodies:
        logger.info("[rotator] body upgrade: %d of %d attempts returned an article",
                    len(bodies), len(attempted))
    return bodies


async def _persist_articles(
    articles: list[NewsArticle], requested_tickers: list[str] | None = None,
) -> int:
    """Write articles to DB with ticker tagging and deduplication.

    Uses the same pattern as news_collector.py:
      - Detects tickers via the shared ticker_extractor module
      - One row per detected ticker for easy querying
      - ON CONFLICT (id) DO NOTHING for deduplication
      - Uses %s placeholders (psycopg compatibility shim)
    """
    from app.collectors.news_collector import (
        _detect_tickers_in_text,
        _get_article_id,
        rank_tickers_for_fanout,
    )

    # Fetch the bodies BEFORE opening the transaction. This used to happen one
    # article at a time inside the `with get_db()` block below, which pinned a
    # database connection for the length of up to ~100 sequential HTTP fetches.
    bodies = await _upgrade_bodies(articles)

    with get_db() as db:
        count = 0

        for article in articles:
            if not article.title:
                continue

            api_summary = article.summary or ""
            summary = bodies.get(article.url or "", "")

            # No body, or one too short to be an article: keep the provider's
            # blurb, exactly as before. An upgrade that fails must never leave
            # the article worse off than not attempting one.
            if (not summary or len(summary) < 150) and len(api_summary) >= 150:
                summary = api_summary

            if is_truncated_content(summary):
                logger.warning(
                    "[rotator][DROP] dropped '%s' from %s — truncated/paywalled (len=%d)",
                    article.title[:60], article.source, len(summary),
                )
                continue

            # Use tickers from API if provided, otherwise detect from full text.
            # These are two DIFFERENT provenances and must not share one label:
            # 'provider' is the vendor's own entity tagging, which we have not
            # verified against the body, while 'detected' means our own
            # `_detect_tickers_in_text` found the symbol in the text. Collapsing
            # them would put an unverified vendor claim behind the same mark the
            # watch desk trusts to arm a trade-enabled wake.
            if article.tickers:
                detected = rank_tickers_for_fanout(
                    article.tickers, requested_tickers, article.title
                )
                attribution = "provider"
            else:
                full_text = f"{article.title} {summary}"
                detected = rank_tickers_for_fanout(
                    await _detect_tickers_in_text(full_text),
                    requested_tickers,
                    article.title,
                )
                attribution = "detected"

            base_id = hashlib.md5(
                f"{article.title}{article.published_at.isoformat()}".encode()
            ).hexdigest()

            # Shared-content dedup: the rotator wrote rows with an EMPTY
            # content_hash, so the DedupEngine used by the finnhub/yfinance/RSS
            # collectors could never see rotator articles — the same story
            # arriving from two providers was double-stored (44 dup groups in
            # one week, all hashless). Compute the same hash and check first.
            # Best-effort: a dedup-engine failure must never block persistence.
            content_hash = ""
            try:
                from app.processors.dedup_engine import DedupEngine
                dedup = DedupEngine(table="news_articles")
                if dedup.is_duplicate(article.title, summary):
                    continue
                content_hash = dedup.compute_hash(article.title, summary)
            except Exception as dedup_err:
                logger.debug("[rotator] dedup check failed (storing anyway): %s", dedup_err)

            from app.collectors.news_collector import quality_at_write, url_fanout_exceeded
            _qs, _qr = quality_at_write(article.title, summary)
            if detected:
                for ticker in detected:
                    if url_fanout_exceeded(db, article.url):
                        break
                    ticker_id = _get_article_id(article.title, ticker)
                    db.execute(
                        """
                        INSERT INTO news_articles
                        (id, ticker, title, publisher, url, published_at, summary, source, collected_at, content_hash, quality_status, quality_reason, ticker_attribution)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                    """,
                        [
                            ticker_id,
                            ticker,
                            article.title[:500],
                            article.source,
                            article.url,
                            article.published_at,
                            summary[:15000],
                            article.source,
                            content_hash,
                            _qs,
                            _qr,
                            # 'provider' or 'detected' per the branch above.
                            # Either way it is never an inherited query ticker:
                            # `rank_tickers_for_fanout` only reorders its input
                            # and drops nothing, so `requested_tickers` cannot
                            # add a symbol that was not already there.
                            attribution,
                        ],
                    )
                    count += 1
            else:
                # General market news — no specific ticker
                article_id = _get_article_id(article.title, None)
                db.execute(
                    """
                    INSERT INTO news_articles
                    (id, ticker, title, publisher, url, published_at, summary, source, collected_at, content_hash, quality_status, quality_reason, ticker_attribution)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                """,
                    [
                        base_id,
                        None,
                        article.title[:500],
                        article.source,
                        article.url,
                        article.published_at,
                        summary[:15000],
                        article.source,
                        content_hash,
                        _qs,
                        _qr,
                        # `ticker` is NULL on this branch, so there is no
                        # attribution to make. Recorded rather than left NULL so
                        # that NULL keeps meaning "legacy row" and nothing else.
                        "general",
                    ],
                )
                count += 1

        return count


# ---------------------------------------------------------------------------
# Main Rotator
# ---------------------------------------------------------------------------


class NewsApiRotator:
    """
    Rotates across all free news API providers. Falls back to next provider
    automatically when a quota is hit or a request fails.

    Usage:
        rotator = NewsApiRotator(tickers=["AAPL", "TSLA"])
        async with rotator:
            articles = await rotator.fetch_news(query="AAPL earnings")
    """

    def __init__(
        self,
        providers: list[ProviderConfig] | None = None,
        tickers: list[str] | None = None,
        include_rss: bool = False,  # RSS already runs in news_collector.py
    ):
        self.providers = providers or build_providers_from_settings()
        self.tickers = tickers or []
        self.include_rss = include_rss
        self._quotas: dict[str, QuotaTracker] = {
            p.name: QuotaTracker(p.daily_limit, p.per_minute_limit)
            for p in self.providers
        }
        self._client: SmartClient | None = None

    async def __aenter__(self) -> "NewsApiRotator":
        self._client = SmartClient(base_delay=1.5, max_retries=3, timeout=20.0)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.__aexit__(*_)

    def _get_client(self) -> SmartClient:
        if not self._client:
            raise RuntimeError("Use NewsApiRotator as an async context manager.")
        return self._client

    async def _fetch_from_provider(
        self,
        provider: ProviderConfig,
        query: str,
    ) -> list[NewsArticle]:
        """Route to scraper-service to fetch from provider."""
        from app.services.scraper_client import scraper_client

        if provider.name == "finnhub":
            # Delegate to the real finnhub_collector module-level function
            from app.collectors.finnhub_collector import collect_news as fh_collect

            # finnhub_collector writes directly to DB and returns count
            # We call it for each ticker and return empty (already persisted)
            for ticker in self.tickers[:5]:
                try:
                    await fh_collect(ticker, days_back=3)
                except Exception as e:
                    logger.warning("[rotator] finnhub failed for %s: %s", ticker, e)
            return []  # Already written to DB by finnhub_collector

        try:
            items = await scraper_client.collect(
                source="finnews",
                req_data={
                    "provider": provider.name,
                    "tickers": self.tickers,
                    "query": query,
                    "limit": 10,
                }
            )
        except Exception as e:
            logger.warning("[rotator] Failed to collect from scraper-service for %s: %s", provider.name, e)
            return []

        articles = []
        for item in items:
            try:
                pub_str = item.get("published_at")
                if pub_str:
                    pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                    if pub.tzinfo is None:
                        # Offset-less timestamps parse naive; the no-timestamp
                        # default below is aware, and mixing the two makes the
                        # newest-first sort raise and abort the whole provider.
                        pub = pub.replace(tzinfo=UTC)
                else:
                    pub = datetime.now(UTC)
            except Exception:
                pub = datetime.now(UTC)
            
            articles.append(
                NewsArticle(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    summary=item.get("summary", ""),
                    source=provider.name,
                    published_at=pub,
                    tickers=item.get("tickers", []),
                    sentiment=item.get("sentiment"),
                )
            )
        return articles

    async def fetch_news(
        self,
        query: str = "stock market",
        max_per_provider: int = 10,
        persist: bool = True,
    ) -> int:
        """
        Fetch news from all available providers in rotation.
        Skips any provider whose quota is exhausted or has no API key.

        Args:
            query: Search query for keyword-based APIs.
            max_per_provider: Max articles to keep per provider.
            persist: If True, write articles to the DB.

        Returns:
            Total number of new articles written to DB.
        """
        all_articles: list[NewsArticle] = []
        seen_urls: set[str] = set()

        # Randomize provider order to spread load
        providers = list(self.providers)
        random.shuffle(providers)

        exhausted_count = 0
        enabled_count = 0

        for provider in providers:
            if not provider.enabled:
                continue
            enabled_count += 1
            quota = self._quotas[provider.name]
            if not await quota.can_use():
                logger.info(
                    "[rotator] Quota exhausted for %s — skipping", provider.name
                )
                exhausted_count += 1
                continue
            try:
                articles = await self._fetch_from_provider(provider, query)
                await quota.consume()
                for a in articles[:max_per_provider]:
                    if a.url and a.url not in seen_urls:
                        seen_urls.add(a.url)
                        all_articles.append(a)
                if articles:
                    logger.info(
                        "[rotator] Fetched %d articles from %s",
                        len(articles),
                        provider.name,
                    )
            except Exception as exc:
                logger.warning("[rotator] Provider %s failed: %s", provider.name, exc)

        if enabled_count > 0 and exhausted_count == enabled_count:
            raise RuntimeError("All news API keys exhausted")

        # Sort newest-first
        # Defensive: any naive published_at from a provider path that skipped
        # coercion would make the aware/naive comparison raise and abort the
        # whole fan-in, so normalize in the sort key too.
        all_articles.sort(
            key=lambda x: x.published_at.replace(tzinfo=UTC) if x.published_at.tzinfo is None else x.published_at,
            reverse=True,
        )

        # Persist to DB
        if persist and all_articles:
            count = await _persist_articles(all_articles, self.tickers)
            logger.info("[rotator] Persisted %d new articles from API providers", count)
            return count

        return 0

    def reset_daily_quotas(self) -> None:
        """Call this at midnight to reset all daily counters."""
        for tracker in self._quotas.values():
            tracker.reset_daily()
        logger.info("[rotator] Daily quotas reset for all providers")


# ---------------------------------------------------------------------------
# Convenience function for pipeline integration
# ---------------------------------------------------------------------------


async def collect_from_all_apis(
    tickers: list[str],
    query: str = "stock market earnings",
) -> int:
    """
    One-shot convenience function for use in the pipeline.
    Fetches from all configured API providers and persists to DB.

    Usage in data_phase.py:
        from app.collectors.news_api_rotator import collect_from_all_apis
        count = await collect_from_all_apis(tickers)
    """
    async with NewsApiRotator(tickers=tickers) as rotator:
        return await rotator.fetch_news(query=query)
