"""
Scrape routes — Single URL and batch scraping endpoints.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

from app.scraper.api.schemas import BatchRequest, ScrapeRequest, ScrapeResponse
from app.scraper.engines.http_engine import HttpEngine
from app.scraper.engines.playwright_engine import PlaywrightEngine
from app.scraper.engines.crawl4ai_engine import Crawl4aiEngine
from app.scraper.engines.vision_engine import VisionEngine
from app.scraper.engines.auto_engine import AutoEngine

logger = logging.getLogger(__name__)
router = APIRouter()

# Engine registry — instantiate once
ENGINES = {
    "http": HttpEngine(),
    "playwright": PlaywrightEngine(),
    "crawl4ai": Crawl4aiEngine(),
    "vision": VisionEngine(),
    "auto": AutoEngine(),
}


@router.post("/scrape", response_model=ScrapeResponse)
async def scrape_url(req: ScrapeRequest):
    """Scrape a single URL using the specified engine.

    Engine options:
      - http: Fast, plain HTTP + BeautifulSoup (default)
      - playwright: Headless Chromium for JS-rendered pages
      - crawl4ai: Advanced crawling with stealth + markdown output
      - vision: Screenshot + VLM OCR (slowest, most powerful)
    """
    engine = ENGINES.get(req.engine)
    if not engine:
        return ScrapeResponse(
            url=req.url,
            success=False,
            error=f"Unknown engine: {req.engine}",
            engine_used=req.engine,
            scraped_at=__import__("datetime").datetime.utcnow(),
        )

    # Merge extract selectors into options
    options = dict(req.options)
    if req.extract:
        options["extract"] = req.extract

    result = await engine.fetch(req.url, options)

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

    async def _scrape_one(job: ScrapeRequest) -> dict:
        async with semaphore:
            engine = ENGINES.get(job.engine)
            if not engine:
                return {"url": job.url, "success": False, "error": f"Unknown engine: {job.engine}"}

            options = dict(job.options)
            if job.extract:
                options["extract"] = job.extract

            result = await engine.fetch(job.url, options)
            return {
                "url": result.url,
                "success": result.success,
                "content": result.content,
                "data": result.data,
                "error": result.error,
                "engine_used": result.engine_used,
                "scraped_at": result.scraped_at.isoformat(),
                "status_code": result.status_code,
            }

    tasks = [_scrape_one(job) for job in req.jobs]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    return {"results": list(results), "count": len(results)}
