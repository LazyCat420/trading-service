"""
playwright_engine.py — Headless browser scraping engine
--------------------------------------------------------
Headless Chromium for JS-rendered pages.
Ported from trading-service's news_playwright.py + youtube_playwright.py.

Strips all trading-specific logic — this is pure browser automation.

Features:
  - Stealth mode (automation flag removal, random viewport, human-like behavior)
  - Cookie/consent banner dismissal
  - CSS selector waiting (wait_for option)
  - Infinite scroll support (scroll option)
  - Screenshot capture (screenshot option)
  - Article text extraction (multiple DOM strategies)
  - Per-domain rate limiting
"""

import asyncio
import logging
import os
import random
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from app.scraper.core.base_engine import BaseEngine
from app.scraper.core.base_result import ScrapeResult
from app.scraper.core.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)

# Upper bound on a caller-supplied navigation timeout.
_MAX_NAV_TIMEOUT_MS = 45_000

# Explicit rather than Playwright's silent 30s default, so a launch failure is
# a named event with a known cost rather than a mystery stall.
_LAUNCH_TIMEOUT_MS = 30_000
_CLOSE_TIMEOUT_S = 15.0

# Concurrent Chromiums per PROCESS.
#
# /scrape/batch built its semaphore PER REQUEST, so the cap bounded one call and
# nothing bounded the sum. Derived, not guessed: pids_limit is 1024 for the whole
# container (docker-compose.yml), the image runs 2 uvicorn workers, and that file
# prices one headless Chromium at ~100 tasks — 1024 / 2 / 100 ≈ 5, so 4 leaves
# room for the rest of the process. The pids cgroup counts THREADS, which is what
# makes a browser cost so much more than it looks.
_MAX_CONCURRENT_BROWSERS = int(os.getenv("SCRAPER_MAX_BROWSERS", "4"))
_browser_semaphore: asyncio.Semaphore | None = None


def _browser_slot():
    """Acquire one of this process's browser slots.

    Built lazily so the semaphore binds to the running loop — the same reason
    ScraperServiceClient defers its own semaphores.
    """
    global _browser_semaphore
    if _browser_semaphore is None:
        _browser_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BROWSERS)
    return _browser_semaphore

# Common article content selectors (ported from news_playwright.py)
ARTICLE_SELECTORS = [
    "article",
    ".article-body", ".article-content", ".article__body",
    ".post-content", ".entry-content", ".story-body",
    '[data-testid="article-body"]',
    ".caas-body",  # Yahoo Finance
    "#article-body", "#story-content",
    "main article", "main .content",
]

# JS to extract article text from DOM (ported from news_playwright.py)
EXTRACT_ARTICLE_JS = """
() => {
    // Strategy 1: <article> tag
    const article = document.querySelector('article');
    if (article) {
        const text = article.innerText;
        if (text && text.length > 200) return text;
    }
    
    // Strategy 2: Common article content selectors
    const selectors = %SELECTORS%;
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el) {
            const text = el.innerText;
            if (text && text.length > 200) return text;
        }
    }
    
    // Strategy 3: Find the largest <p> cluster
    const paragraphs = Array.from(document.querySelectorAll('p'));
    if (paragraphs.length > 3) {
        const text = paragraphs.map(p => p.innerText.trim()).filter(t => t.length > 30).join('\\n');
        if (text.length > 200) return text;
    }
    
    // Strategy 4: main tag
    const main = document.querySelector('main');
    if (main) return main.innerText;
    
    return document.body.innerText || null;
}
""".replace("%SELECTORS%", str(ARTICLE_SELECTORS))


class PlaywrightEngine(BaseEngine):
    """Headless Chromium scraping engine using Playwright.

    Best for:
    - JavaScript-rendered pages (SPAs, dynamic content)
    - Cloudflare/bot-protected pages
    - Pages requiring scroll to load content
    - Screenshot capture for vision pipeline
    """

    async def fetch(self, url: str, options: dict[str, Any]) -> ScrapeResult:
        domain = urlparse(url).netloc

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return ScrapeResult(
                url=url, success=False, content=None, data={},
                error="playwright not installed — run: pip install playwright && playwright install chromium --with-deps",
                engine_used="playwright", scraped_at=datetime.utcnow(),
            )

        screenshot_b64 = None
        status_code = None

        try:
            async with rate_limiter.acquire(domain):
                async with _browser_slot():
                    async with async_playwright() as p:
                        browser = None
                        try:
                            # INSIDE the try. `launch()` used to sit above it, so
                            # a launch that spawned Chromium and then raised —
                            # its 30s readiness timeout, under exactly the memory
                            # and pid pressure this container is sized for — left
                            # a live browser with no handle and no finally. The
                            # close-on-failure test only ever drove a page.goto
                            # failure, so it passed with that hole open.
                            browser = await p.chromium.launch(
                                headless=True,
                                args=["--disable-blink-features=AutomationControlled"],
                                timeout=_LAUNCH_TIMEOUT_MS,
                            )
                            from app.scraper.core.session_manager import (
                                DEFAULT_UA, browser_headers,
                            )

                            context = await browser.new_context(
                                user_agent=DEFAULT_UA,
                                viewport={
                                    "width": 1280 + random.randint(0, 100),
                                    "height": 900 + random.randint(0, 50),
                                },
                                locale="en-US",
                                timezone_id="America/Los_Angeles",
                                # Chromium sets Sec-Fetch-* itself, but not
                                # Accept-Language/Sec-Ch-Ua consistently in headless.
                                extra_http_headers={
                                    k: v for k, v in browser_headers().items()
                                    if k.lower() not in ("user-agent", "accept-encoding")
                                },
                            )
                            page = await context.new_page()

                            # Apply stealth to bypass Cloudflare
                            try:
                                from playwright_stealth import Stealth
                                await Stealth().apply_stealth_async(page)
                            except Exception as stealth_err:
                                logger.warning(f"[playwright] Failed to apply stealth: {stealth_err}")

                            # Block heavy resources to speed up loading
                            allow_images = options.get("allow_images", False)
                            if allow_images:
                                await page.route(
                                    "**/*.{mp4,webm,woff,woff2}",
                                    lambda route: route.abort(),
                                )
                            else:
                                await page.route(
                                    "**/*.{png,jpg,jpeg,gif,svg,mp4,webm,woff,woff2}",
                                    lambda route: route.abort(),
                                )

                            # Navigate. CLAMPED: `timeout` comes straight off the
                            # wire, and an unbounded value pins a browser (and an
                            # async slot, and ~100 pids) for as long as the caller
                            # asks — 24h is a legal int.
                            timeout_ms = min(int(options.get("timeout", 20000) or 20000), _MAX_NAV_TIMEOUT_MS)
                            # KEEP the response. page.goto returns one, and this
                            # engine used to throw it away — so status_code was
                            # None on every Playwright result, which made
                            # auto_engine's `_served_ok` a tautology at this tier
                            # (None counts as served) and its post-Playwright
                            # 404/410 check unreachable dead code.
                            nav = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                            if nav is not None:
                                status_code = nav.status

                            # Stealth: human-like behavior
                            await page.mouse.move(
                                random.randint(200, 800), random.randint(200, 600)
                            )
                            await page.wait_for_timeout(800 + random.randint(0, 500))

                            # Dismiss cookie banners / modals
                            for dismiss_sel in [
                                "button:has-text('Accept')",
                                "button:has-text('Accept all')",
                                "button:has-text('I agree')",
                                "button:has-text('Continue')",
                                "[aria-label='Close']",
                            ]:
                                try:
                                    btn = page.locator(dismiss_sel)
                                    if await btn.count() > 0:
                                        await btn.first.click(timeout=1500)
                                        await page.wait_for_timeout(500)
                                        break
                                except Exception:
                                    continue

                            # Wait for specific selector if requested
                            wait_for = options.get("wait_for")
                            if wait_for:
                                try:
                                    await page.wait_for_selector(wait_for, timeout=10000)
                                except Exception:
                                    logger.warning(f"[playwright] wait_for selector '{wait_for}' timed out")

                            # Scroll to bottom if requested
                            if options.get("scroll"):
                                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                                await page.wait_for_timeout(2000)

                            # Wait for content to render
                            await page.wait_for_timeout(1000)

                            # Screenshot if requested
                            if options.get("screenshot"):
                                import base64
                                screenshot_bytes = await page.screenshot(type="png", full_page=False)
                                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

                            # Extract data via CSS selectors
                            data: dict[str, Any] = {}
                            extract_map = options.get("extract")
                            if extract_map and isinstance(extract_map, dict):
                                for field_name, selector in extract_map.items():
                                    try:
                                        elements = page.locator(selector)
                                        count = await elements.count()
                                        values = []
                                        for i in range(count):
                                            text = await elements.nth(i).inner_text()
                                            if text.strip():
                                                values.append(text.strip())
                                        data[field_name] = values
                                    except Exception:
                                        data[field_name] = []

                            # Extract raw HTML or article text
                            raw_html = options.get("raw_html", False)
                            evaluate_js = options.get("evaluate")
                            if evaluate_js:
                                try:
                                    eval_res = await page.evaluate(evaluate_js)
                                    if isinstance(eval_res, dict):
                                        data.update(eval_res)
                                    else:
                                        data["evaluate_result"] = eval_res
                                    # Populate content_data so length check passes
                                    content_data = str(eval_res) if eval_res else "Evaluated successfully"
                                    if len(content_data) < 100:
                                        # Ensure it passes the 100 char limit for success
                                        content_data = (await page.evaluate("() => document.body.innerText")) or content_data
                                except Exception as eval_err:
                                    logger.error(f"[playwright] Error in custom evaluate: {eval_err}")
                                    raise eval_err
                            elif raw_html:
                                content_data = await page.content()
                            else:
                                content_data = await page.evaluate(EXTRACT_ARTICLE_JS)

                        finally:
                            # A failed scrape must not orphan a Chromium:
                            # uvicorn is PID 1 in this container and never
                            # reaps, so every orphan became an unkillable
                            # zombie until the host ran out of PIDs. 20,179 of
                            # them exhausted the HOST's pid space and took the
                            # NAS down twice.
                            #
                            # `if browser` because launch() itself can now fail
                            # in here; shield() because the caller's timeout
                            # cancels this coroutine, and a cancelled close()
                            # leaks the very browser this block exists to
                            # reclaim.
                            if browser is not None:
                                try:
                                    await asyncio.shield(
                                        asyncio.wait_for(browser.close(), timeout=_CLOSE_TIMEOUT_S)
                                    )
                                except Exception as close_err:  # noqa: BLE001
                                    logger.error(
                                        "[playwright] browser.close() failed for %s: %r — "
                                        "this is the zombie path, check pids.current",
                                        url, close_err,
                                    )

            # Clean up/format content
            content = None
            if content_data:
                if raw_html:
                    content = content_data
                else:
                    content = re.sub(r"\n{3,}", "\n\n", content_data).strip()
                    max_chars = options.get("max_chars", 15000)
                    if len(content) > max_chars:
                        content = content[:max_chars]

            return ScrapeResult(
                url=url,
                success=bool(content and len(content) > 100),
                content=content,
                data=data,
                error=None,
                engine_used="playwright",
                scraped_at=datetime.utcnow(),
                status_code=status_code,
                screenshot_b64=screenshot_b64,
            )

        except Exception as e:
            logger.error(f"[playwright] Error scraping {url}: {e}")
            return ScrapeResult(
                url=url, success=False, content=None, data={},
                error=str(e), engine_used="playwright",
                scraped_at=datetime.utcnow(),
            )

    async def health_check(self) -> bool:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = None
                try:
                    browser = await p.chromium.launch(headless=True, timeout=_LAUNCH_TIMEOUT_MS)
                    page = await browser.new_page()
                    await page.goto("https://example.com", timeout=10000)
                    title = await page.title()
                    return bool(title)
                finally:
                    # Same rule as fetch(): a probe that cannot reach the
                    # network must not leave a browser behind — including one
                    # whose launch() is what failed.
                    if browser is not None:
                        try:
                            await asyncio.shield(
                                asyncio.wait_for(browser.close(), timeout=_CLOSE_TIMEOUT_S)
                            )
                        except Exception:  # noqa: BLE001
                            logger.error("[playwright] health_check close() failed")
        except Exception:
            return False
