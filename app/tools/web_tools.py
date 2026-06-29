import logging
import json
from app.tools.registry import registry
from app.services.scraper_client import scraper_client

logger = logging.getLogger(__name__)

@registry.register(
    name="search_web",
    description="Perform a web search using DuckDuckGo. Returns results with titles, URLs, and snippets.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 5).",
            },
        },
        "required": ["query"],
    }
)
async def search_web(query: str, limit: int = 5) -> str:
    """Search the web using DuckDuckGo via scraper-service."""
    logger.info(f"[WebTools] Python search_web called with query: {query}")
    try:
        items = await scraper_client.collect("duckduckgo", {"query": query, "limit": limit})
        results = []
        for item in items:
            results.append({
                "title": item.get("title", "No Title"),
                "url": item.get("url", ""),
                "snippet": item.get("snippet", item.get("description", ""))
            })
        return json.dumps({
            "status": "success",
            "query": query,
            "results": results
        })
    except Exception as e:
        logger.error(f"[WebTools] search_web error: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "query": query,
            "results": [],
            "message": str(e)
        })

@registry.register(
    name="web_search",
    description="Perform a web search using DuckDuckGo. Returns results with titles, URLs, and snippets.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 5).",
            },
        },
        "required": ["query"],
    }
)
async def web_search(query: str, limit: int = 5) -> str:
    """Alias for search_web."""
    return await search_web(query, limit)

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

async def hermes_web_research(query: str) -> str:
    # Fallback/compatibility definition
    res = await search_web(query)
    return str(res)
