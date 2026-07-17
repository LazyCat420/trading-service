import logging
import asyncio
from typing import Any

logger = logging.getLogger(__name__)


class ScraperServiceClient:
    """In-process client for the absorbed scraper (formerly the scraper-service HTTP API).

    The scraper's engines and collectors now live under ``app.scraper`` and run in
    this process, so ``.scrape()`` / ``.collect()`` call the in-process seam
    (``app.scraper.service``) instead of POSTing to ``scraper-service:8001``. The
    method signatures and return contracts are unchanged, so every existing caller
    (``app/collectors/*`` wrappers, ``app/tools/web_tools.py``, ...) keeps working.

    A per-source ``asyncio.Semaphore`` still bounds concurrency — this matters more
    now that scraping runs inside the trading worker process alongside Chromium.
    """

    def __init__(self, base_url: str | None = None):
        # base_url is retained only for backwards-compatible construction; it is
        # unused now that calls are in-process.
        self.base_url = base_url
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def _get_semaphore(self, source: str) -> asyncio.Semaphore:
        """Lazily initialize semaphores so they bind to the active event loop."""
        if source not in self._semaphores:
            self._semaphores[source] = asyncio.Semaphore(5)
        return self._semaphores[source]

    async def scrape(self, url: str, engine: str = "http", options: dict | None = None) -> dict | None:
        """Scrape a single URL in-process.

        Returns the parsed result dict (with ``success``/``content`` keys) or None
        on failure — same contract as the old ``POST /scrape``.
        """
        from app.scraper.service import scrape as _scrape

        sem = self._get_semaphore("news")
        try:
            async with sem:
                data = await _scrape(url, engine=engine, options=options or {})

            if data.get("success"):
                return data
            logger.warning(f"[scraper_client] Scrape failed for {url}: {data.get('error')}")
            return None
        except Exception as e:
            logger.error(f"[scraper_client] Unexpected error scraping {url}: {e}")
            return None

    async def collect(self, source: str, req_data: dict) -> list[dict[str, Any]]:
        """Collect from a source in-process.

        Returns the list of collected items — same contract as the old
        ``POST /collect`` (``data["items"]``).
        """
        from app.scraper.service import collect as _collect

        sem = self._get_semaphore(source)
        try:
            async with sem:
                data = await _collect(source, req_data)

            if data.get("error"):
                logger.warning(f"[scraper_client] Collect failed for {source}: {data['error']}")

            return data.get("items", [])
        except Exception as e:
            logger.error(f"[scraper_client] Unexpected error collecting from {source}: {e}")
            return []


scraper_client = ScraperServiceClient()
