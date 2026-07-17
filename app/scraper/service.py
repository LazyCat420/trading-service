"""In-process seam for the absorbed scraper (formerly the standalone scraper-service).

Lets `app/services/scraper_client.py` and the FastAPI routers share one code path
with no HTTP hop. The scrape/collect request handlers live in
`app/scraper/api/routes/*`; this module adapts them to plain dict in/out so the
historical `ScraperServiceClient.scrape()/.collect()` contract is preserved.

Imports of the route modules are deferred to call time — `routes/scrape.py`
instantiates the engine registry at import (needs bs4; playwright/crawl4ai are
themselves imported lazily inside each engine's `fetch()`), so keeping this module
import-light avoids coupling trading-service startup to the browser stack.
"""

from typing import Any

from app.scraper.api.schemas import CollectRequest, ScrapeRequest


async def scrape(url: str, engine: str = "http", options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scrape a single URL in-process. Returns a ScrapeResponse-shaped dict
    (`{url, success, content, data, error, engine_used, scraped_at, ...}`)."""
    from app.scraper.api.routes.scrape import scrape_url as _handler

    resp = await _handler(ScrapeRequest(url=url, engine=engine, options=options or {}))
    return resp.model_dump()


async def collect(source: str, req_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a collection in-process (non-streaming). Returns a CollectResponse-shaped
    dict (`{source, count, items, error}`)."""
    from app.scraper.api.routes.collect import collect as _handler

    payload = {**(req_data or {}), "source": source, "stream": False}
    resp = await _handler(CollectRequest(**payload))
    # The non-stream path always returns a CollectResponse (never a StreamingResponse).
    return resp.model_dump()
