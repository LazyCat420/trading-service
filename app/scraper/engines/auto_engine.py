"""
auto_engine.py — Orchestrated fallback scraping engine
-------------------------------------------------------
Runs a sequential pipeline:
  1. http: Fast, plain GET.
  2. playwright: Headless JS execution.
  3. vision: Screen screenshot OCR (last resort).
"""

import logging
from datetime import datetime
from typing import Any

from app.scraper.core.base_engine import BaseEngine
from app.scraper.core.base_result import ScrapeResult
from app.scraper.engines.http_engine import HttpEngine
from app.scraper.engines.playwright_engine import PlaywrightEngine
from app.scraper.engines.vision_engine import VisionEngine

logger = logging.getLogger(__name__)

BLOCK_SIGNATURES = [
    "please enable javascript",
    "please enable cookies",
    "subscribe to continue",
    "verify you are human",
    "pardon our interruption",
    "are you a robot",
    "access to this page has been denied",
    "press & hold to confirm",
    "ddg-captcha",
    "cloudflare",
    "enable cookies and javascript",
    # Interstitials seen in production that this list did not cover, so
    # is_blocked_content() returned False and the block page was returned as a
    # successful scrape. Bloomberg's "let us know you're not a robot" page is
    # 464 chars — past the len>150 gate below — and was being stored as the
    # article body, quietly poisoning the corpus instead of failing loudly.
    "access is temporarily restricted",
    "we detected unusual activity",
    "unusual activity from your device",
    "not a robot",
    "let us know you're not a robot",
    "checking your browser",
    "request blocked",
    "access denied",
    "temporarily blocked",
    "rate limit exceeded",
    # Bloomberg serves its corporate boilerplate footer — the same 279 chars for
    # every URL — in place of the article when it refuses us. It trips no other
    # signature and clears the length gate, so without this it is stored as the
    # article body. Site-specific, but so are "pardon our interruption" (Distil)
    # and "ddg-captcha" already in this list.
    "connecting decision makers to a dynamic network of information",
]


class AutoEngine(BaseEngine):
    """Orchestrated scraping engine that falls back to more powerful engines when blocked."""

    def __init__(self):
        self.http_engine = HttpEngine()
        self.playwright_engine = PlaywrightEngine()
        self.vision_engine = VisionEngine()

    def is_blocked_content(self, text: str) -> bool:
        """Check if retrieved text contains block signatures indicating captcha or bot shield."""
        if not text:
            return True
        text_lower = text.lower()
        for sig in BLOCK_SIGNATURES:
            if sig in text_lower:
                return True
        return False

    async def fetch(self, url: str, options: dict[str, Any]) -> ScrapeResult:
        # Phase 1: HTTP
        logger.info(f"[auto] Trying HTTP engine for {url}")
        res = await self.http_engine.fetch(url, options)
        
        # If success, status code is valid, length is sufficient, and not blocked
        if res.success and res.content and len(res.content) > 150:
            if res.status_code in [200, 201, 202] and not self.is_blocked_content(res.content):
                logger.info(f"[auto] HTTP engine succeeded for {url}")
                res.engine_used = "auto (http)"
                return res
            else:
                logger.info(f"[auto] HTTP engine returned blocked content or status {res.status_code}")
        else:
            _why = res.error or f"status={res.status_code} len={len(res.content or '')}"
            logger.info(f"[auto] HTTP engine failed for {url}: {_why}")

        # Phase 2: Playwright
        logger.info(f"[auto] Escalating to Playwright engine for {url}")
        res = await self.playwright_engine.fetch(url, options)
        
        if res.success and res.content and len(res.content) > 150:
            if not self.is_blocked_content(res.content):
                logger.info(f"[auto] Playwright engine succeeded for {url}")
                res.engine_used = "auto (playwright)"
                return res
            else:
                logger.info(f"[auto] Playwright engine returned blocked or captcha content")
        else:
            logger.info(f"[auto] Playwright engine failed: {res.error}")

        # Phase 3: Vision Engine (last resort fallback)
        logger.info(f"[auto] Escalating to Vision engine for {url}")
        res = await self.vision_engine.fetch(url, options)
        
        if res.success:
            logger.info(f"[auto] Vision engine succeeded for {url}")
            res.engine_used = "auto (vision)"
            return res
        else:
            logger.info(f"[auto] Vision engine failed: {res.error}")

        # If all fail, return the last result
        res.engine_used = "auto (failed)"
        return res

    async def health_check(self) -> bool:
        # Make sure basic sub-engines check out
        return await self.http_engine.health_check()
