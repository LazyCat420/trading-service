"""
auto_engine.py — Orchestrated fallback scraping engine
-------------------------------------------------------
Runs a sequential pipeline:
  1. http: Fast, plain GET.
  2. playwright: Headless JS execution.

A third phase — screenshot + VLM OCR — was removed on 2026-08-09. It could
not succeed: it imported the trading app's LLM config layer, which
scraper-service's deploy.sh deliberately does not ship, so every call raised
``ImportError`` and every failed scrape paid ~16s to reach it. Its own
measured benchmarks also undercut the case for repairing it — on a genuine
bot-wall (the pages http and playwright cannot get) OCR correctly returned
"no content", because a bot-wall renders as a bot-wall in a screenshot too.
Host discovery, the one piece worth keeping, moved to
``app.services.vllm_hosts``.
"""

import logging
from datetime import datetime
from typing import Any

from app.scraper.core.base_engine import BaseEngine
from app.scraper.core.base_result import ScrapeResult
from app.scraper.core import content_quality
from app.scraper.core.failure_cache import PERMANENT_STATUSES, failure_cache
from app.scraper.engines.http_engine import HttpEngine
from app.scraper.engines.playwright_engine import PlaywrightEngine

logger = logging.getLogger(__name__)


def _served_ok(status_code: int | None) -> bool:
    """Did the site actually serve this page, rather than refuse it?

    Playwright reports no status, so ``None`` counts as served. An explicit
    non-2xx does not: a 429 cooldown page is short, and counting it as "this
    domain only serves headers" would let our own request rate earn a working
    site a 24-hour skip.
    """
    return status_code is None or 200 <= status_code < 300

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
    # Verbatim from stocktitan.net under load. A cooldown page is short, so
    # without these it read as "this domain only serves headers" and counted
    # toward the domain skip — punishing a working site for our own request
    # rate. Rate limiting is the most transient block there is.
    "too many requests",
    "you're loading pages faster",
    "you are loading pages faster",
    "please wait about",
    "automatic cooldown",
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

    def is_blocked_content(self, text: str) -> bool:
        """Check if retrieved text contains block signatures indicating captcha or bot shield."""
        if not text:
            return True
        text_lower = text.lower()
        for sig in BLOCK_SIGNATURES:
            if sig in text_lower:
                return True
        return False

    def _dead(self, url: str, reason: str) -> ScrapeResult:
        """A result for a URL known to be permanently gone."""
        return ScrapeResult(
            url=url, success=False, content=None, data={},
            error=f"URL is permanently unavailable ({reason})",
            engine_used="auto (skipped)", scraped_at=datetime.utcnow(),
        )

    def _thin(self, url: str, res: ScrapeResult) -> ScrapeResult:
        """A 200 that carried a headline and a lede, and nothing else."""
        return ScrapeResult(
            url=url, success=False, content=res.content, data=res.data or {},
            error=content_quality.thin_reason(res.content),
            engine_used="auto (thin)", scraped_at=datetime.utcnow(),
            status_code=res.status_code,
        )

    async def fetch(self, url: str, options: dict[str, Any]) -> ScrapeResult:
        # Phase 0: have we already established this URL is gone? Skipping is
        # the whole point — one 410 article was re-walked 157 times over 12
        # days because nothing remembered the answer.
        cached = failure_cache.check(url)
        if cached:
            logger.info("[auto] Skipping %s — known dead (%s)", url, cached)
            return self._dead(url, cached)

        # Phase 0b: does this site ever give us an article? A domain that has
        # returned nothing but a headline every time, and a real article never,
        # is not worth a request — measured, not hardcoded, and re-tested once
        # a day so a site that stops paywalling comes back on its own.
        domain = content_quality.domain_of(url)
        skip = failure_cache.should_skip_domain(domain)
        if skip:
            logger.info("[auto] Skipping %s — %s", url, skip)
            return ScrapeResult(
                url=url, success=False, content=None, data={},
                error=f"domain yields no articles ({skip})",
                engine_used="auto (skipped)", scraped_at=datetime.utcnow(),
            )

        # Phase 1: HTTP
        logger.info(f"[auto] Trying HTTP engine for {url}")
        res = await self.http_engine.fetch(url, options)

        # A 404/410 is a fact about the URL, not a transient miss. Escalating
        # to a browser cannot conjure a deleted page, so stop here and
        # remember it. Transient failures (5xx, timeouts, bot-walls) fall
        # through to Playwright as before — those do recover.
        if res.status_code in PERMANENT_STATUSES:
            reason = f"HTTP {res.status_code}"
            failure_cache.record(url, reason)
            logger.info("[auto] %s returned %s — not retrying", url, reason)
            return self._dead(url, reason)

        # If success, status code is valid, length is sufficient, and not blocked
        if res.success and res.content and len(res.content) > 150:
            if res.status_code in [200, 201, 202] and not self.is_blocked_content(res.content):
                # A 200 is not an article. Escalating to Playwright cannot
                # unlock a paywall either, so judge the body here: a headline
                # plus a lede is a failure, and one the domain is charged for.
                if content_quality.is_thin(res.content):
                    failure_cache.record_quality(domain, good=False)
                    logger.info("[auto] %s — %s", url,
                                content_quality.thin_reason(res.content))
                    return self._thin(url, res)
                failure_cache.record_quality(domain, good=True)
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
                if content_quality.is_thin(res.content):
                    # Only a page the site actually SERVED counts against it.
                    # Playwright reports no status, so None is accepted; an
                    # explicit non-2xx (429, 503) is a transient refusal, and
                    # counting it would let a rate limit earn a 24h skip.
                    if _served_ok(res.status_code):
                        failure_cache.record_quality(domain, good=False)
                    logger.info("[auto] %s — %s", url,
                                content_quality.thin_reason(res.content))
                    return self._thin(url, res)
                failure_cache.record_quality(domain, good=True)
                logger.info(f"[auto] Playwright engine succeeded for {url}")
                res.engine_used = "auto (playwright)"
                return res
            else:
                logger.info(f"[auto] Playwright engine returned blocked or captcha content")
        else:
            logger.info(f"[auto] Playwright engine failed: {res.error}")

        # Playwright is the last phase. A 404/410 that only surfaces here (a
        # soft redirect to a "gone" page, say) is still permanent.
        if res.status_code in PERMANENT_STATUSES:
            failure_cache.record(url, f"HTTP {res.status_code}")

        # If all fail, return the last result
        res.engine_used = "auto (failed)"
        return res

    async def health_check(self) -> bool:
        # Make sure basic sub-engines check out
        return await self.http_engine.health_check()
