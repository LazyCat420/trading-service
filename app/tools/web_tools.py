import logging
import json
from app.tools.registry import registry
from app.services.scraper_client import scraper_client

logger = logging.getLogger(__name__)

@registry.register(
    name="scrape_url",
    description="Scrape the main text content from a URL.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to scrape."}
        },
        "required": ["url"],
    }
)
async def scrape_url(url: str) -> str:
    """Scrape text content from a URL via scraper-service."""
    logger.info(f"[WebTools] Python scrape_url called with url: {url}")
    try:
        data = await scraper_client.scrape(url, engine="http")
        if data and data.get("success"):
            return json.dumps({
                "status": "success",
                "url": url,
                "content": data.get("content", "")[:8000]
            })
        return json.dumps({
            "status": "error",
            "url": url,
            "message": data.get("error", "Unknown scrape failure") if data else "Null response"
        })
    except Exception as e:
        logger.error(f"[WebTools] scrape_url error: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "url": url,
            "message": str(e)
        })
