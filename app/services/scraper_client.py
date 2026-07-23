import logging
import asyncio
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class ScraperServiceClient:
    """HTTP client for the standalone scraper-service (:8001).

    The scraper was extracted back out of this process into its own
    domain-agnostic service (``scraper-service``), so ``.scrape()`` / ``.collect()``
    POST to its HTTP API instead of running Chromium in the trading worker. The
    method signatures and return contracts are unchanged, so every existing
    caller (``app/collectors/*`` wrappers, ``app/tools/web_tools.py``, the
    registry web-scrape tools bridged from lazy-tool, ...) keeps working.

    The scraper source of truth still lives in ``app.scraper`` here — scraper-service
    build-copies it — but this process no longer imports or runs it. Base URL comes
    from ``settings.SCRAPER_SERVICE_URL`` (default ``http://scraper-service:8001``).

    A per-source ``asyncio.Semaphore`` bounds how many concurrent requests we fan
    out to the scraper so a burst of collectors can't stampede it.
    """

    # Generous: a vision-OCR scrape can run 30-40s per page, and /scrape/batch or
    # a multi-feed /collect fans several of those out server-side.
    _TIMEOUT_S = 300.0

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.SCRAPER_SERVICE_URL).rstrip("/")
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        # Failure ledger so callers can distinguish "scraper returned nothing"
        # from "scraper was unreachable". The except→return []/None contract
        # below laundered a TOTAL outage (unresolvable host on
        # cycle-v3-1784769797) into a ✅ "collected 0 articles" sweep.
        self.failures = 0
        self.last_error: str | None = None

    def reset_failures(self) -> None:
        self.failures = 0
        self.last_error = None

    def _get_semaphore(self, source: str) -> asyncio.Semaphore:
        """Lazily initialize semaphores so they bind to the active event loop."""
        if source not in self._semaphores:
            self._semaphores[source] = asyncio.Semaphore(5)
        return self._semaphores[source]

    async def scrape(self, url: str, engine: str = "http", options: dict | None = None) -> dict | None:
        """Scrape a single URL via scraper-service ``POST /scrape``.

        Returns the parsed result dict (with ``success``/``content`` keys) or None
        on failure — same contract as the in-process version it replaced.
        """
        sem = self._get_semaphore("news")
        payload = {"url": url, "engine": engine, "options": options or {}}
        try:
            async with sem:
                async with httpx.AsyncClient(timeout=self._TIMEOUT_S) as client:
                    resp = await client.post(f"{self.base_url}/scrape", json=payload)
                    resp.raise_for_status()
                    data = resp.json()

            if data.get("success"):
                return data
            logger.warning(f"[scraper_client] Scrape failed for {url}: {data.get('error')}")
            return None
        except Exception as e:
            self.failures += 1
            self.last_error = str(e)
            logger.error(f"[scraper_client] Unexpected error scraping {url}: {e}")
            return None

    async def collect(self, source: str, req_data: dict) -> list[dict[str, Any]]:
        """Collect from a source via scraper-service ``POST /collect``.

        Returns the list of collected items — same contract as the in-process
        version (``data["items"]``).
        """
        sem = self._get_semaphore(source)
        payload = {"source": source, **(req_data or {})}
        try:
            async with sem:
                async with httpx.AsyncClient(timeout=self._TIMEOUT_S) as client:
                    resp = await client.post(f"{self.base_url}/collect", json=payload)
                    resp.raise_for_status()
                    data = resp.json()

            if data.get("error"):
                logger.warning(f"[scraper_client] Collect failed for {source}: {data['error']}")

            return data.get("items", [])
        except Exception as e:
            self.failures += 1
            self.last_error = str(e)
            logger.error(f"[scraper_client] Unexpected error collecting from {source}: {e}")
            return []


scraper_client = ScraperServiceClient()
