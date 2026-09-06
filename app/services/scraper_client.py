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
    # Sources whose collectors are rate-limited upstream and legitimately slow.
    _SLOW_SOURCE_TIMEOUT_S = {"reddit-purge": 900.0}

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.SCRAPER_SERVICE_URL).rstrip("/")
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        # Failure ledger so callers can distinguish "scraper returned nothing"
        # from "scraper was unreachable". The except→return []/None contract
        # below laundered a TOTAL outage (unresolvable host on
        # cycle-v3-1784769797) into a ✅ "collected 0 articles" sweep.
        self.failures = 0
        self.calls = 0
        self.last_error: str | None = None

    def reset_failures(self) -> None:
        """Zero the process-wide ledger.

        Prefer :meth:`sweep`. This instance is a module-level singleton shared
        by nine call sites, so a reset here zeroes the count for every sweep
        running concurrently — a nightly StockTwits pass and an agent's
        scrape_url tool call both write to the same counter the discovery
        sweep is about to read.
        """
        self.failures = 0
        self.calls = 0
        self.last_error = None

    def sweep(self) -> "SweepRecord":
        """A per-sweep view of the ledger, measured as a DELTA.

        ``reset_failures()`` + read cannot separate this sweep's failures from
        a concurrent caller's. A delta can::

            rec = scraper_client.sweep()
            total = await collect_all()
            if rec.failed:
                log.warning("%d of %d calls errored (%s)", rec.failures, rec.calls, rec.last_error)
        """
        return SweepRecord(self)

    def _note_failure(self, source: str, detail: str) -> None:
        self.failures += 1
        self.last_error = f"{source}: {detail}"

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
        self.calls += 1
        try:
            async with sem:
                async with httpx.AsyncClient(timeout=self._TIMEOUT_S) as client:
                    resp = await client.post(f"{self.base_url}/scrape", json=payload)
                    resp.raise_for_status()
                    data = resp.json()

            if data.get("success"):
                return data
            # Return the payload rather than None even on failure. AutoEngine
            # produces five distinct verdicts — "auto (skipped)" + HTTP 410,
            # "auto (skipped)" + domain-yields-no-articles, "auto (thin)",
            # "auto (failed)" — and collapsing all of them to None threw away
            # `engine_used` and `error` at the door. Every caller gates on
            # `.get("success")` (news_collector:256, web_tools:24,
            # web_search:590), so the dict is safe to hand back and lets them
            # tell a skipped domain from a bot-wall from a dead URL.
            #
            # NOT counted as a failure: the scraper answered. A per-URL miss is
            # not an outage, and counting it would make the outage signal fire
            # on every paywalled article.
            logger.warning(f"[scraper_client] Scrape failed for {url}: {data.get('error')}")
            return data
        except Exception as e:
            self._note_failure(url, repr(e))
            logger.error(f"[scraper_client] Unexpected error scraping {url}: {e!r}")
            return None

    async def collect(self, source: str, req_data: dict) -> list[dict[str, Any]]:
        """Collect from a source via scraper-service ``POST /collect``.

        Returns the list of collected items — same contract as the in-process
        version (``data["items"]``).
        """
        sem = self._get_semaphore(source)
        payload = {"source": source, **(req_data or {})}
        # reddit-purge walks listings + per-thread comment feeds at reddit's
        # 1-req/10s RSS budget — a legitimate sweep runs 5-10 minutes and was
        # dying on the generic 300s timeout (as a laundered empty error).
        timeout_s = self._SLOW_SOURCE_TIMEOUT_S.get(source, self._TIMEOUT_S)
        self.calls += 1
        try:
            async with sem:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    resp = await client.post(f"{self.base_url}/collect", json=payload)
                    resp.raise_for_status()
                    data = resp.json()

            if data.get("error"):
                # This is a SERVER-SIDE fault, not an empty result.
                # collect.py answers 200 with an `error` field only from its
                # blanket `except Exception` — a collector that raised. That is
                # the shape the 13-day vision outage had, and the shape a
                # partial-copy ImportError has today. Counting it only in the
                # transport `except` below left `failures == 0` through a total
                # collector outage, so pipeline_service stamped the sweep
                # ✅ "collected 0 articles".
                self._note_failure(source, str(data["error"]))
                logger.warning(f"[scraper_client] Collect failed for {source}: {data['error']}")

            return data.get("items", [])
        except Exception as e:
            self._note_failure(source, repr(e))
            # repr, not str: httpx/asyncio timeout exceptions stringify to ""
            # and the failure arrives as a blank cause (the laundered-timeout
            # trap that hid this exact defect).
            logger.error(f"[scraper_client] Unexpected error collecting from {source}: {e!r}")
            return []


class SweepRecord:
    """Failures attributable to one sweep, as a delta on the shared ledger.

    Every value is computed live against the snapshot taken at construction, so
    a concurrent caller's failures never land in this sweep's count.
    """

    __slots__ = ("_client", "_f0", "_c0")

    def __init__(self, client: ScraperServiceClient):
        self._client = client
        self._f0 = client.failures
        self._c0 = client.calls

    @property
    def failures(self) -> int:
        return self._client.failures - self._f0

    @property
    def calls(self) -> int:
        return self._client.calls - self._c0

    @property
    def failed(self) -> bool:
        return self.failures > 0

    @property
    def failure_rate(self) -> float:
        """Fraction of this sweep's calls that errored. 0.0 when none were made."""
        return (self.failures / self.calls) if self.calls else 0.0

    @property
    def last_error(self) -> str | None:
        return self._client.last_error if self.failed else None

    def __repr__(self) -> str:
        return (f"<SweepRecord {self.failures}/{self.calls} failed "
                f"({self.failure_rate:.0%}) last={self.last_error!r}>")


scraper_client = ScraperServiceClient()
