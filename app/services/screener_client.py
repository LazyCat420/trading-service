"""Client for trading-client's market-screener snapshot API (:8888).

Mirrors scraper_client conventions: httpx async, env-overridable base URL,
returns dict | None (never raises), explicit failure state so callers can
distinguish "no matches" from "screener unreachable" — a dead backend must
never be laundered into an empty result.
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)


class ScreenerClient:
    # Warm-cache queries answer in ~50ms; a cold snapshot rebuild takes ~10s.
    _TIMEOUT_S = 45.0

    def __init__(self):
        self.base_url = os.getenv(
            "TRADING_CLIENT_URL", "http://trading-client:8888"
        ).rstrip("/")
        self.last_error: str | None = None
        self._sem: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(4)
        return self._sem

    async def query(
        self,
        filters: list[str] | None = None,
        sort: str | None = None,
        direction: str = "desc",
        limit: int = 15,
        columns: list[str] | None = None,
    ) -> dict | None:
        """Run a screener query. Returns the API payload dict, a dict with
        an 'error' key for a 4xx (bad field/op — the message lists valid
        fields so the caller can self-correct), or None if unreachable."""
        params: list[tuple[str, str]] = []
        for spec in filters or []:
            params.append(("f", spec))
        if sort:
            params.append(("sort", sort))
            params.append(("dir", direction))
        params.append(("limit", str(limit)))
        if columns:
            params.append(("columns", ",".join(columns)))
        url = f"{self.base_url}/api/v1/screener/snapshot"
        try:
            async with self._semaphore():
                async with httpx.AsyncClient(timeout=self._TIMEOUT_S) as client:
                    resp = await client.get(url, params=params)
            if resp.status_code == 400:
                detail = resp.json().get("detail", resp.text[:500])
                self.last_error = None
                return {"error": detail}
            resp.raise_for_status()
            self.last_error = None
            return resp.json()
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning("[screener_client] query failed: %s", self.last_error)
            return None


screener_client = ScreenerClient()
