"""
Scrape routes — Single URL and batch scraping endpoints.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter

from app.scraper.api.schemas import BatchRequest, ScrapeRequest, ScrapeResponse
from app.scraper.core.url_guard import UnsafeUrlError, check_url, sanitize_options
from app.scraper.engines.http_engine import HttpEngine
from app.scraper.engines.playwright_engine import PlaywrightEngine
from app.scraper.engines.crawl4ai_engine import Crawl4aiEngine
from app.scraper.engines.auto_engine import AutoEngine

logger = logging.getLogger(__name__)
router = APIRouter()

# Server-side deadline per URL, below ScraperServiceClient._TIMEOUT_S (300s) so
# the caller always hears an answer rather than timing out on its own side while
# this process keeps burning a browser for a response nobody will read.
_ROUTE_DEADLINE_S = 120.0

# Engine registry — instantiate once.
# "vision" (screenshot + VLM OCR) was removed on 2026-08-09: it depended on
# the trading app's LLM config layer, which this image does not ship, so it
# raised ImportError on every call. See auto_engine's module docstring.
ENGINES = {
    "http": HttpEngine(),
    "playwright": PlaywrightEngine(),
    "crawl4ai": Crawl4aiEngine(),
    "auto": AutoEngine(),
}


@router.post("/scrape", response_model=ScrapeResponse)
async def scrape_url(req: ScrapeRequest):
    """Scrape a single URL using the specified engine.

    Engine options:
      - http: Fast, plain HTTP + BeautifulSoup (default)
      - playwright: Headless Chromium for JS-rendered pages
      - crawl4ai: Advanced crawling with stealth + markdown output
      - auto: http, then playwright on failure
    """
    engine = ENGINES.get(req.engine)
    if not engine:
        return ScrapeResponse(
            url=req.url,
            success=False,
            error=f"Unknown engine: {req.engine}",
            engine_used=req.engine,
            scraped_at=datetime.utcnow(),
        )

    try:
        check_url(req.url)
    except UnsafeUrlError as exc:
        return ScrapeResponse(
            url=req.url, success=False, error=str(exc),
            engine_used=req.engine, scraped_at=datetime.utcnow(),
        )

    # Caller-supplied `evaluate`/`js_code` ran arbitrary JS in the fetched
    # page's origin and returned the result — a same-origin read the caller's
    # own browser could not perform. Dropped here, once, for every engine.
    options = sanitize_options(req.options)
    if req.extract:
        options["extract"] = req.extract

    # A hard ceiling below the client's 300s. Without it the auto ladder's eight
    # independent timeouts sum to ~100s per URL, and a caller-set
    # options["timeout"] could hold a Chromium for as long as it liked.
    try:
        result = await asyncio.wait_for(engine.fetch(req.url, options), timeout=_ROUTE_DEADLINE_S)
    except asyncio.TimeoutError:
        return ScrapeResponse(
            url=req.url, success=False,
            error=f"scrape exceeded the {_ROUTE_DEADLINE_S:.0f}s server deadline",
            engine_used=req.engine, scraped_at=datetime.utcnow(),
        )

    return ScrapeResponse(
        url=result.url,
        success=result.success,
        content=result.content,
        data=result.data,
        error=result.error,
        engine_used=result.engine_used,
        scraped_at=result.scraped_at,
        status_code=result.status_code,
        screenshot_b64=getattr(result, "screenshot_b64", None),
    )


@router.post("/scrape/batch")
async def scrape_batch(req: BatchRequest):
    """Scrape multiple URLs concurrently.

    Uses asyncio.Semaphore to limit concurrency to max_concurrency.
    """
    semaphore = asyncio.Semaphore(req.max_concurrency)
    results: list[dict[str, Any]] = []

    def _fail(job: ScrapeRequest, error: str) -> dict:
        return ScrapeResponse(
            url=job.url, success=False, error=error,
            engine_used=job.engine, scraped_at=datetime.utcnow(),
        ).model_dump(mode="json")

    async def _scrape_one(job: ScrapeRequest) -> dict:
        async with semaphore:
            engine = ENGINES.get(job.engine)
            if not engine:
                return _fail(job, f"Unknown engine: {job.engine}")
            try:
                check_url(job.url)
            except UnsafeUrlError as exc:
                return _fail(job, str(exc))

            options = sanitize_options(job.options)
            if job.extract:
                options["extract"] = job.extract

            try:
                result = await asyncio.wait_for(
                    engine.fetch(job.url, options), timeout=_ROUTE_DEADLINE_S
                )
            except asyncio.TimeoutError:
                return _fail(job, f"scrape exceeded the {_ROUTE_DEADLINE_S:.0f}s server deadline")

            # Built from ScrapeResponse rather than hand-assembled: the hand-
            # built dict silently dropped screenshot_b64, so the same job
            # answered differently depending on which endpoint you sent it to.
            return ScrapeResponse(
                url=result.url,
                success=result.success,
                content=result.content,
                data=result.data,
                error=result.error,
                engine_used=result.engine_used,
                scraped_at=result.scraped_at,
                status_code=result.status_code,
                screenshot_b64=getattr(result, "screenshot_b64", None),
            ).model_dump(mode="json")

    tasks = [_scrape_one(job) for job in req.jobs]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    return {"results": list(results), "count": len(results)}
