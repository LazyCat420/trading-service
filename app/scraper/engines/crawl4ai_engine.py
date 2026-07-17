"""
crawl4ai_engine.py — Advanced crawling engine
-----------------------------------------------
Wraps crawl4ai v0.8.5. Ported from trading-service's crawl4ai_config.py.
"""

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from app.scraper.core.base_engine import BaseEngine
from app.scraper.core.base_result import ScrapeResult
from app.scraper.core.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)


class Crawl4aiEngine(BaseEngine):
    """Advanced crawling engine powered by crawl4ai.

    Best for complex pages, batch scraping, overlay/paywall bypass.
    Falls back gracefully if crawl4ai is not installed.
    """

    async def fetch(self, url: str, options: dict[str, Any]) -> ScrapeResult:
        domain = urlparse(url).netloc

        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
            from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
            from crawl4ai.content_filter_strategy import PruningContentFilter
        except ImportError:
            return ScrapeResult(
                url=url, success=False, content=None, data={},
                error="crawl4ai not installed — run: pip install crawl4ai",
                engine_used="crawl4ai", scraped_at=datetime.utcnow(),
            )

        fast = options.get("fast", False)
        max_chars = options.get("max_chars", 15000)

        try:
            browser_cfg = BrowserConfig(
                browser_type="chromium", headless=True,
                viewport_width=1280, viewport_height=800,
                ignore_https_errors=True, verbose=False,
                enable_stealth=True, avoid_ads=True,
                text_mode=fast, light_mode=fast,
                memory_saving_mode=fast,
            )

            crawl_kwargs = dict(
                word_count_threshold=30,
                excluded_tags=["script", "style", "nav", "footer", "header"],
                only_text=False,
                remove_overlay_elements=True,
                remove_consent_popups=True,
                scan_full_page=options.get("scroll", True),
                scroll_delay=0.3, max_scroll_steps=10,
                flatten_shadow_dom=True,
                wait_until="domcontentloaded",
                page_timeout=30000,
                wait_for_images=True,
                delay_before_return_html=0.5,
                screenshot=options.get("screenshot", False),
                score_links=True,
                exclude_social_media_links=True,
                process_iframes=True,
                max_retries=2, verbose=False,
                cache_mode=CacheMode.ENABLED,
                markdown_generator=DefaultMarkdownGenerator(
                    content_filter=PruningContentFilter(
                        threshold=0.4, threshold_type="fixed",
                        min_word_threshold=30,
                    )
                ),
            )

            if options.get("css_selector"):
                crawl_kwargs["css_selector"] = options["css_selector"]
            if options.get("js_code"):
                crawl_kwargs["js_code"] = options["js_code"]

            crawl_cfg = CrawlerRunConfig(**crawl_kwargs)

            async with rate_limiter.acquire(domain):
                async with AsyncWebCrawler(config=browser_cfg) as crawler:
                    r = await crawler.arun(url=url, config=crawl_cfg)

            if r.success:
                text = ""
                if hasattr(r, "fit_markdown") and r.fit_markdown:
                    text = r.fit_markdown
                elif r.markdown:
                    text = r.markdown
                text = text[:max_chars] if text else ""

                return ScrapeResult(
                    url=url,
                    success=len(text) > 50,
                    content=text, data={}, error=None,
                    engine_used="crawl4ai",
                    scraped_at=datetime.utcnow(),
                    screenshot_b64=getattr(r, "screenshot", None),
                    links=getattr(r, "links", []) or [],
                    media=getattr(r, "media", []) or [],
                    metadata=getattr(r, "metadata", {}) or {},
                )
            else:
                return ScrapeResult(
                    url=url, success=False, content=None, data={},
                    error=getattr(r, "error_message", "Unknown crawl4ai error"),
                    engine_used="crawl4ai", scraped_at=datetime.utcnow(),
                )

        except Exception as e:
            logger.error(f"[crawl4ai] Error scraping {url}: {e}")
            return ScrapeResult(
                url=url, success=False, content=None, data={},
                error=str(e), engine_used="crawl4ai",
                scraped_at=datetime.utcnow(),
            )

    async def health_check(self) -> bool:
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
            cfg = BrowserConfig(headless=True, text_mode=True, light_mode=True)
            run = CrawlerRunConfig(scan_full_page=False)
            async with AsyncWebCrawler(config=cfg) as crawler:
                r = await crawler.arun(url="https://example.com", config=run)
                return r.success
        except Exception:
            return False
