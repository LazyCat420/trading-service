"""
http_engine.py — Primary scraping engine
-----------------------------------------
Plain HTTPX requests + BeautifulSoup extraction.
Good for: server-rendered HTML, JSON APIs, RSS feeds.

Ported from trading-service SmartClient + news_collector patterns.
Strips all trading-specific logic — this is pure HTTP fetching.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.scraper.core.base_engine import BaseEngine
from app.scraper.core.base_result import ScrapeResult
from app.scraper.core.rate_limiter import rate_limiter
from app.scraper.core.session_manager import session_manager

logger = logging.getLogger(__name__)


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
    # Strip blocks
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<svg[^>]*>.*?</svg>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    # Strip individual tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Normalize spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


def _extract_text_from_html(html: str, max_chars: int = 15000) -> str:
    """Extract readable text from HTML using trafilatura (ported from news_collector).

    Falls back to BeautifulSoup if trafilatura is not installed or fails.
    """
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
            html,
            include_links=False,
            include_images=False,
            include_tables=False,
            no_fallback=False,
        )
        if text and len(text) > 50:
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:max_chars]
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: BeautifulSoup
    try:
        soup = BeautifulSoup(html, "lxml")
        # Remove script/style tags
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars] if text else ""
    except Exception:
        return ""


class HttpEngine(BaseEngine):
    """Plain HTTP scraping engine using shared session manager.

    Supports:
    - Raw HTML fetching
    - CSS selector extraction via BeautifulSoup
    - JSON API responses (auto-detected from Content-Type)
    - Article text extraction via trafilatura
    - Per-domain rate limiting
    """

    async def fetch(self, url: str, options: dict[str, Any]) -> ScrapeResult:
        domain = urlparse(url).netloc
        try:
            async with rate_limiter.acquire(domain):
                # options is not popped, but we can access headers or cookies here if needed
                response = await session_manager.client.get(url)

            content_type = response.headers.get("content-type", "")

            # JSON responses — return data directly
            if "application/json" in content_type:
                try:
                    json_data = response.json()
                    return ScrapeResult(
                        url=url,
                        success=True,
                        content=response.text,
                        data=json_data if isinstance(json_data, dict) else {"items": json_data},
                        error=None,
                        engine_used="http",
                        scraped_at=datetime.utcnow(),
                        status_code=response.status_code,
                    )
                except Exception:
                    pass

            # HTML responses — parse and extract
            html = response.text
            data: dict[str, Any] = {}

            # Apply CSS selector extraction if requested
            extract_map = options.get("extract")
            if extract_map and isinstance(extract_map, dict):
                soup = BeautifulSoup(html, "lxml")
                for field_name, selector in extract_map.items():
                    elements = soup.select(selector)
                    data[field_name] = [el.get_text(strip=True) for el in elements]

            # Extract article text
            extracted_text = _extract_text_from_html(html)
            if not extracted_text:
                extracted_text = _clean_html_fallback(html)

            return ScrapeResult(
                url=url,
                success=True,
                content=extracted_text,
                data=data,
                error=None,
                engine_used="http",
                scraped_at=datetime.utcnow(),
                status_code=response.status_code,
            )

        except Exception as e:
            logger.error(f"[http] Error fetching {url}: {e}")
            return ScrapeResult(
                url=url,
                success=False,
                content=None,
                data={},
                error=str(e),
                engine_used="http",
                scraped_at=datetime.utcnow(),
            )

    async def health_check(self) -> bool:
        try:
            r = await session_manager.client.get(
                "https://httpbin.org/get", timeout=5
            )
            return r.status_code == 200
        except Exception:
            return False
