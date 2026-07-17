"""
news_collector.py — Domain-agnostic news/RSS collection
---------------------------------------------------------
Ported from trading-service's news_collector.py.
All trading-specific logic (ticker extraction, company mapping,
DB writes) has been REMOVED. This collector knows HOW to pull
news articles from RSS feeds — the caller decides WHICH feeds.

Libraries: feedparser, httpx, trafilatura, cloudscraper
No API key needed for RSS.
"""

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import feedparser

from app.scraper.core.rate_limiter import rate_limiter
from app.scraper.core.session_manager import session_manager

logger = logging.getLogger(__name__)


@dataclass
class NewsArticle:
    """Normalized news article data."""
    id: str
    title: str
    url: str
    summary: str
    publisher: str
    published_at: datetime | None
    source_type: str  # 'rss', 'api', 'scrape'


import json
from bs4 import BeautifulSoup


def _extract_seeking_alpha_ssr(html: str) -> str | None:
    """Extract and format Seeking Alpha article contents from embedded JSON state."""
    match = re.search(r"window\.SSR_DATA\s*=\s*(\{.*?\});?\s*</script>", html, re.DOTALL)
    if not match:
        match = re.search(r"window\.SSR_DATA\s*=\s*(\{.*?\}),?\s*\n", html, re.DOTALL)
    if not match:
        return None
    try:
        data_str = match.group(1)
        data = json.loads(data_str)
        article = data.get("article", {}).get("response", {}).get("data", {}).get("attributes", {})
        content_html = article.get("content")
        if content_html:
            soup = BeautifulSoup(content_html, "html.parser")
            text = soup.get_text(separator=" ", strip=True)
            
            # Extract Quick Insights if available
            insights = article.get("quickInsights", [])
            if insights:
                insights_text = []
                for ins in sorted(insights, key=lambda x: x.get("order", 0)):
                    q = ins.get("question", "")
                    a = ins.get("answer", "")
                    if q and a:
                        insights_text.append(f"Q: {q}\nA: {a}")
                if insights_text:
                    text = text + "\n\nQuick Insights:\n" + "\n".join(insights_text)
            return text.strip()
    except Exception as e:
        logger.warning(f"Failed to parse Seeking Alpha SSR_DATA: {e}")
    return None


def _clean_html_fallback(html: str, max_chars: int = 15000) -> str:
    """Fallback utility to strip HTML tags, script blocks, and style blocks using regex."""
    if not html:
        return ""
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<svg[^>]*>.*?</svg>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


def _extract_text_from_html(html: str, max_chars: int = 15000) -> str:
    """Extract readable text from HTML using trafilatura."""
    if not html:
        return ""

    # Seeking Alpha JSON extraction
    if "seekingalpha" in html.lower() or "ssr_data" in html.lower():
        sa_text = _extract_seeking_alpha_ssr(html)
        if sa_text:
            return sa_text[:max_chars]

    # Try trafilatura first (best article extraction)
    try:
        import trafilatura
        text = trafilatura.extract(
            html, include_links=False, include_images=False,
            include_tables=False, no_fallback=False,
        )
        if text and len(text) > 50:
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"\s+", " ", text).strip()

            # Quality gate: filter out bot/paywall pages
            failure_sigs = [
                "please enable javascript", "please enable cookies",
                "subscribe to continue", "verify you are human",
                "pardon our interruption", "are you a robot",
            ]
            for sig in failure_sigs:
                if sig in text.lower():
                    return ""

            return text[:max_chars]
    except ImportError:
        pass
    except Exception:
        pass

    # Try BeautifulSoup fallback
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        if text and len(text) > 50:
            # Check quality gate
            failure_sigs = [
                "please enable javascript", "please enable cookies",
                "subscribe to continue", "verify you are human",
                "pardon our interruption", "are you a robot",
            ]
            for sig in failure_sigs:
                if sig in text.lower():
                    return ""
            return text[:max_chars]
    except Exception:
        pass

    # Final fallback
    cleaned = _clean_html_fallback(html, max_chars)
    # Check quality gate
    failure_sigs = [
        "please enable javascript", "please enable cookies",
        "subscribe to continue", "verify you are human",
        "pardon our interruption", "are you a robot",
    ]
    for sig in failure_sigs:
        if sig in cleaned.lower():
            return ""
    return cleaned


async def _scrape_article_body(url: str, max_chars: int = 15000) -> str:
    """Fetch article URL and extract body text."""
    try:
        r = await session_manager.client.get(url, timeout=15.0)
        if r.status_code == 200:
            text = _extract_text_from_html(r.text, max_chars)
            if text and len(text) > 50:
                return text
    except Exception:
        pass
    return ""


class NewsCollector:
    """Collects news articles from RSS feeds and URLs.

    Two modes:
      1. collect_feed() — Parse a single RSS feed
      2. collect_feeds() — Parse multiple RSS feeds with rate limiting

    All feed URLs come from the CALLER. This class has zero
    domain knowledge about which feeds to watch.
    """

    async def collect_feed(
        self,
        feed_name: str,
        feed_url: str,
        scrape_bodies: bool = True,
        min_summary_length: int = 100,
    ) -> list[NewsArticle]:
        """Fetch and parse a single RSS feed.

        Args:
            feed_name: Display name for the feed (used as publisher)
            feed_url: URL of the RSS/Atom feed
            scrape_bodies: If True, scrape article body when RSS summary is too short
            min_summary_length: Minimum chars for a summary to pass quality gate
        """
        articles: list[NewsArticle] = []

        try:
            domain = re.search(r"https?://([^/]+)", feed_url)
            domain_str = domain.group(1) if domain else "unknown"

            async with rate_limiter.acquire(domain_str):
                r = await session_manager.client.get(feed_url, timeout=30.0)

            if r.status_code != 200:
                logger.warning(f"[news] {feed_name}: HTTP {r.status_code}")
                return articles

            feed = feedparser.parse(r.text)

            if not feed.entries:
                logger.warning(f"[news] {feed_name}: 0 entries")
                return articles

            for entry in feed.entries:
                title = entry.get("title", "").strip()
                if not title:
                    continue

                # Parse published date
                published_at = self._parse_feed_date(entry)
                url = entry.get("link", "")
                summary = entry.get("summary", "").strip()

                # Scrape body if summary is too short
                if scrape_bodies and url and (not summary or "..." in summary or len(summary) < 150):
                    body = await _scrape_article_body(url)
                    if body:
                        summary = body

                # Quality gate
                if len(summary) < min_summary_length:
                    continue

                # Generate deterministic ID
                id_str = f"{title}{published_at.isoformat() if published_at else ''}"
                article_id = hashlib.md5(id_str.encode()).hexdigest()

                articles.append(NewsArticle(
                    id=article_id,
                    title=title[:500],
                    url=url,
                    summary=summary,
                    publisher=feed_name,
                    published_at=published_at,
                    source_type="rss",
                ))

        except Exception as e:
            logger.error(f"[news] {feed_name} error: {e}")

        return articles

    async def collect_feeds(
        self,
        feeds: dict[str, str],
        scrape_bodies: bool = True,
        pace_seconds: float = 2.0,
    ) -> list[NewsArticle]:
        """Collect articles from multiple RSS feeds with rate limiting.

        Args:
            feeds: Dict of {feed_name: feed_url}
            scrape_bodies: If True, scrape article bodies when summaries are short
            pace_seconds: Delay between feeds to avoid rate limiting
        """
        all_articles: list[NewsArticle] = []

        for name, url in feeds.items():
            try:
                articles = await self.collect_feed(name, url, scrape_bodies)
                all_articles.extend(articles)
                if articles:
                    logger.info(f"[news] {name}: {len(articles)} articles")
            except Exception as e:
                logger.error(f"[news] {name}: {e}")

            await asyncio.sleep(pace_seconds)

        logger.info(f"[news] Total: {len(all_articles)} articles from {len(feeds)} feeds")
        return all_articles

    def _parse_feed_date(self, entry) -> datetime | None:
        """Parse date from feedparser entry."""
        for attr in ("published_parsed", "updated_parsed"):
            parsed = getattr(entry, attr, None)
            if parsed:
                try:
                    return datetime(
                        parsed.tm_year, parsed.tm_mon, parsed.tm_mday,
                        parsed.tm_hour, parsed.tm_min, parsed.tm_sec,
                    )
                except Exception:
                    pass
        return datetime.utcnow()


def _serialize_article(article: NewsArticle) -> dict:
    """Convert NewsArticle to JSON-safe dict for API responses."""
    return {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "summary": article.summary,
        "publisher": article.publisher,
        "published_at": article.published_at.isoformat() if article.published_at else None,
        "source_type": article.source_type,
    }
