"""
http_engine.py — Primary scraping engine
-----------------------------------------
Plain HTTPX requests + BeautifulSoup extraction.
Good for: server-rendered HTML, JSON APIs, RSS feeds.

Ported from trading-service SmartClient + news_collector patterns.
Strips all trading-specific logic — this is pure HTTP fetching.
"""

import asyncio
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

from app.utils.text_utils import _extract_seeking_alpha_ssr

logger = logging.getLogger(__name__)


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


def _extract_with_selectors(html: str, extract_map: dict[str, str]) -> dict[str, Any]:
    """CSS-selector extraction. Sync and CPU-bound — call it in a thread."""
    data: dict[str, Any] = {}
    soup = BeautifulSoup(html, "lxml")
    for field_name, selector in extract_map.items():
        try:
            elements = soup.select(selector)
        except Exception:  # noqa: BLE001 — a caller's bad selector is not a crash
            data[field_name] = []
            continue
        data[field_name] = [el.get_text(strip=True) for el in elements]
    return data


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
                # Honour the caller's budget. This used to fetch with the
                # client default (30s) whatever `options["timeout"]` said, so
                # the auto ladder's first leg alone could outlast the caller's
                # entire deadline and every escalation past it was wasted work.
                timeout_s = options.get("timeout")
                if timeout_s:
                    # Callers speak milliseconds (playwright's unit); httpx wants seconds.
                    response = await session_manager.client.get(url, timeout=float(timeout_s) / 1000.0)
                else:
                    response = await session_manager.client.get(url)

            content_type = response.headers.get("content-type", "")

            # JSON responses — return data directly
            if "application/json" in content_type:
                try:
                    json_data = response.json()
                    # Same rule as the HTML branch below: a JSON error body is
                    # not a successful scrape, though it stays in `data` for
                    # callers that want to read the error.
                    ok = 200 <= response.status_code < 300
                    return ScrapeResult(
                        url=url,
                        success=ok,
                        content=response.text,
                        data=json_data if isinstance(json_data, dict) else {"items": json_data},
                        error=None if ok else f"HTTP {response.status_code}",
                        engine_used="http",
                        scraped_at=datetime.utcnow(),
                        status_code=response.status_code,
                    )
                except Exception:
                    pass

            # HTML responses — parse and extract
            html = response.text
            data: dict[str, Any] = {}

            # Everything below parses HTML, and a news page is routinely
            # multi-megabyte (thestreet.com measured at 2.3MB). trafilatura runs
            # a full lxml parse plus a readability/justext fallback, then
            # BeautifulSoup may parse it AGAIN, then _clean_html_fallback runs
            # five DOTALL regex sweeps — all of it CPU-bound C and all of it
            # used to sit directly on the event loop, in a process whose Docker
            # healthcheck has a 5s timeout. One C call starves the loop whole:
            # a worker thread is the only thing that frees it.
            extract_map = options.get("extract")
            if extract_map and isinstance(extract_map, dict):
                data = await asyncio.to_thread(_extract_with_selectors, html, extract_map)

            extracted_text = await asyncio.to_thread(_extract_text_from_html, html)
            if not extracted_text:
                extracted_text = await asyncio.to_thread(_clean_html_fallback, html)

            # `success` must reflect the status. This returned True for ANY
            # status, so a 410 came back as
            #   success=True, status_code=410,
            #   content="This content has been permanently removed."
            # AutoEngine re-checked the status itself and was unaffected, but a
            # direct `engine="http"` caller had only its own length guard
            # between an error page and the article body — and an error page
            # with more boilerplate than that guard allows would be stored as
            # the article. Callers that want the body of a non-2xx response
            # still get it in `content`.
            ok = 200 <= response.status_code < 300
            return ScrapeResult(
                url=url,
                success=ok,
                content=extracted_text,
                data=data,
                error=None if ok else f"HTTP {response.status_code}",
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
