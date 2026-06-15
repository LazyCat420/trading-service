import logging
import json
import aiohttp
import asyncio
from bs4 import BeautifulSoup
from app.tools.registry import registry
from tenacity import retry, stop_after_attempt, wait_fixed

logger = logging.getLogger(__name__)




@registry.register(
    name="scrape_url",
    description="Scrape the main text content from a URL. Use this to read articles or SEC filings.",
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The URL to scrape."}},
        "required": ["url"],
    },
    tier=0,
    source="aiohttp",
)
async def scrape_url(url: str) -> str:
    """
    Scrape text content from a web page using aiohttp and BeautifulSoup.
    """
    logger.info(f"[WebTools] Scraping URL: {url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status != 200:
                    return f"Error: Received status code {response.status}"
                html = await response.text()

                soup = BeautifulSoup(html, "html.parser")
                # Remove scripts and styles
                for script in soup(["script", "style", "nav", "footer", "header"]):
                    script.decompose()

                text = soup.get_text(separator=" ", strip=True)
                # Truncate to avoid massive context window usage
                truncated_text = text[:8000]

                return json.dumps(
                    {
                        "status": "success",
                        "url": url,
                        "content": truncated_text,
                        "truncated": len(text) > 8000,
                    }
                )
    except Exception as e:
        logger.error(f"[WebTools] Scraping failed for {url}: {e}")
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="search_web",
    description="Search the web for news and information. Returns a list of titles, URLs, and snippets.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "num_results": {"type": "integer", "description": "Number of results to return.", "default": 3}
        },
        "required": ["query"],
    },
    tier=0,
    source="ddg",
)
async def search_web(query: str, num_results: int = 3) -> str:
    """
    Search the web for news and information.
    """
    from app.services.web_search import searcher
    logger.info(f"[WebTools] Searching web for: {query}")
    try:
        results = await searcher.search(query, max_results=num_results)
        if not results:
            return "No results found."
        
        formatted = []
        for i, r in enumerate(results, 1):
            formatted.append(f"[{i}] {r.title}\nURL: {r.url}\nSnippet: {r.snippet}\n")
        return "\n".join(formatted)
    except Exception as e:
        logger.error(f"[WebTools] Search failed for '{query}': {e}")
        return f"Error: {e}"





