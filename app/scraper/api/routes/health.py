"""
Health check routes.

⚠ /health is what Docker polls, so it decides whether this container can ever
be reported unhealthy. It used to return a hardcoded literal — no branch, no
input, nothing it could report but "healthy" — which meant uvicorn accepting a
socket was the ONLY failure the health gate could see. Chromium refusing to
launch, the container hitting its pid ceiling, the failure cache silently
disabled, every collector ImportError-ing: all of it read `Up N days (healthy)`
with restarts=0. That is precisely how the vision outage ran 13 days unnoticed.

So the check now touches the two dependencies that are cheap AND load-bearing.
It deliberately does NOT launch a browser or make a network call: this runs
every 30s with a 5s timeout, and a health probe that is itself expensive
becomes the outage. The expensive checks stay on /health/engines.
"""

import logging
import os

from fastapi import APIRouter, Response

from app.scraper.core.failure_cache import failure_cache
from app.scraper.core.session_manager import session_manager
from app.scraper.engines.http_engine import HttpEngine

logger = logging.getLogger(__name__)

router = APIRouter()

SERVICE_VERSION = "1.0.0"

# Stamped at build time by deploy.sh (--build-arg GIT_SHA). Without it there is
# no way to ask a running container which commit it was built from — and the
# staged app/ tree is gitignored, so `git diff` in the deploy repo is trivially
# clean whatever the image contains.
BUILD_SHA = os.getenv("BUILD_SHA", "unknown")
BUILD_TIME = os.getenv("BUILD_TIME", "unknown")


@router.get("/health")
async def health(response: Response):
    """Liveness + the two dependencies a scrape cannot proceed without."""
    checks: dict[str, bool] = {}

    # The shared httpx client every engine and collector fetches through.
    try:
        client = session_manager.client
        checks["http_client"] = client is not None and not client.is_closed
    except Exception:  # noqa: BLE001
        checks["http_client"] = False

    # The learned domain-quality record. Degraded (memory-only) is not fatal —
    # it costs one wasted fetch per URL, it does not fail a scrape — so it is
    # REPORTED but does not flip the status. Saying so is the point: nothing
    # logged when this silently switched itself off.
    try:
        store = failure_cache.store
        checks["failure_cache"] = bool(store is not None and store.enabled)
    except Exception:  # noqa: BLE001
        checks["failure_cache"] = False

    healthy = checks["http_client"]
    if not healthy:
        response.status_code = 503
        logger.error("[health] unhealthy: %s", checks)

    return {
        "status": "healthy" if healthy else "unhealthy",
        "service": "scraper-service",
        "version": SERVICE_VERSION,
        "build": {"sha": BUILD_SHA, "time": BUILD_TIME},
        "checks": checks,
    }


@router.get("/health/engines")
async def engine_health():
    """Deep health check — tests each engine's connectivity.

    Launches real browsers, so it is far too expensive for the 30s Docker
    healthcheck. Call it by hand, or from a watch that can afford ~5s.
    """
    results = {}

    # HTTP engine
    try:
        http = HttpEngine()
        results["http"] = await http.health_check()
    except Exception:
        results["http"] = False

    # Playwright (optional)
    try:
        from app.scraper.engines.playwright_engine import PlaywrightEngine
        pw = PlaywrightEngine()
        results["playwright"] = await pw.health_check()
    except Exception:
        results["playwright"] = False

    # crawl4ai (optional)
    try:
        from app.scraper.engines.crawl4ai_engine import Crawl4aiEngine
        c4 = Crawl4aiEngine()
        results["crawl4ai"] = await c4.health_check()
    except Exception:
        results["crawl4ai"] = False

    return {
        "status": "healthy" if results.get("http") else "degraded",
        "engines": results,
        "build": {"sha": BUILD_SHA, "time": BUILD_TIME},
    }
