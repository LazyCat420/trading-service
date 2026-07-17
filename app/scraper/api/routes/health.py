"""
Health check routes.
"""

from fastapi import APIRouter

from app.scraper.core.session_manager import session_manager
from app.scraper.engines.http_engine import HttpEngine

router = APIRouter()


@router.get("/health")
async def health():
    """Basic health check — returns service status."""
    return {
        "status": "healthy",
        "service": "scraper-service",
        "version": "0.1.0",
    }


@router.get("/health/engines")
async def engine_health():
    """Deep health check — tests each engine's connectivity."""
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
    }
