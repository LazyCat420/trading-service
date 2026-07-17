"""
duckduckgo_collector.py
-----------------------
Collector for DuckDuckGo web search. Uses the html.duckduckgo.com/html POST endpoint,
with an automated Playwright fallback if rate-limited or blocked.
"""

import httpx
import logging
import urllib.parse
from bs4 import BeautifulSoup
from typing import Dict, Any, List

from app.scraper.engines.playwright_engine import PlaywrightEngine

logger = logging.getLogger(__name__)

DUCKDUCKGO_HTML_BASE = "https://html.duckduckgo.com/html/"
DUCKDUCKGO_BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

class DuckDuckGoCollector:
    """Collector to search DuckDuckGo directly."""
    
    def __init__(self):
        self.playwright = PlaywrightEngine()

    async def search(self, query: str, limit: int = 10, date_restrict: str = None) -> List[Dict[str, Any]]:
        """
        Search DuckDuckGo using HTTP POST, fallback to Playwright GET.
        date_restrict can be: d1 (day), w1 (week), m1 (month), etc.
        """
        results = await self._search_http(query, limit, date_restrict)
        if not results:
            logger.info(f"[duckduckgo] HTTP search returned 0 results or failed for '{query}'. Trying Playwright fallback...")
            results = await self._search_playwright(query, limit, date_restrict)
        
        # limit is enforced here again just in case
        return results[:limit]

    async def _search_http(self, query: str, limit: int, date_restrict: str) -> List[Dict[str, Any]]:
        form_data = {"q": query}
        
        if date_restrict:
            # Map common date restrict strings to DDG's 'df' param
            ddg_date_map = {
                "d1": "d",
                "d7": "w",
                "w1": "w",
                "w2": "m",
                "m1": "m",
                "m3": "m",
                "y1": "y",
            }
            if date_restrict in ddg_date_map:
                form_data["df"] = ddg_date_map[date_restrict]

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    DUCKDUCKGO_HTML_BASE,
                    data=form_data,
                    headers={
                        "User-Agent": DUCKDUCKGO_BROWSER_USER_AGENT,
                        "Accept": "text/html",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                    follow_redirects=True
                )
                response.raise_for_status()
                html = response.text
                
                if "ddg-captcha" in html or len(html) < 2000:
                    logger.warning("[duckduckgo] HTTP Search returned captcha or blocked page.")
                    return []

                return self._parse_html(html, limit)
        except Exception as e:
            logger.error(f"[duckduckgo] HTTP search error: {e}")
            return []

    async def _search_playwright(self, query: str, limit: int, date_restrict: str) -> List[Dict[str, Any]]:
        safe_query = urllib.parse.quote_plus(query)
        url = f"{DUCKDUCKGO_HTML_BASE}?q={safe_query}"
        
        if date_restrict:
            ddg_date_map = {
                "d1": "d", "d7": "w", "w1": "w", "w2": "m", "m1": "m", "m3": "m", "y1": "y"
            }
            if date_restrict in ddg_date_map:
                url += f"&df={ddg_date_map[date_restrict]}"

        options = {
            "raw_html": True,
            "wait_for": ".result"
        }
        
        try:
            result = await self.playwright.fetch(url, options)
            if result.success and result.content:
                return self._parse_html(result.content, limit)
            else:
                logger.error(f"[duckduckgo] Playwright fallback failed: {result.error}")
        except Exception as e:
            logger.error(f"[duckduckgo] Playwright fallback exception: {e}")
        
        return []

    def _parse_html(self, html: str, limit: int) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        results = []
        
        for result_div in soup.select(".result"):
            if len(results) >= limit:
                break
                
            title_a = result_div.select_one(".result__a")
            if not title_a:
                continue
                
            raw_title = title_a.get_text(strip=True)
            raw_href = title_a.get("href", "")
            
            snippet_elem = result_div.select_one(".result__snippet")
            raw_snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""
            
            url_elem = result_div.select_one(".result__url")
            raw_display_url = url_elem.get_text(strip=True) if url_elem else ""
            
            resolved_url = self._decode_redirect_url(raw_href)
            if raw_title and resolved_url:
                results.append({
                    "title": raw_title,
                    "url": resolved_url,
                    "snippet": raw_snippet,
                    "displayUrl": raw_display_url or self._extract_hostname(resolved_url)
                })
                
        return results

    def _decode_redirect_url(self, raw_href: str) -> str:
        try:
            if raw_href.startswith("//"):
                raw_href = f"https:{raw_href}"
            
            parsed = urllib.parse.urlparse(raw_href)
            qs = urllib.parse.parse_qs(parsed.query)
            if "uddg" in qs:
                return qs["uddg"][0]
                
            if raw_href.startswith("http"):
                return raw_href
        except Exception:
            pass
        return ""

    def _extract_hostname(self, url: str) -> str:
        try:
            return urllib.parse.urlparse(url).hostname or ""
        except Exception:
            return ""
