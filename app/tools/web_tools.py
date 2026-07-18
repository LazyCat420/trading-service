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

@registry.register(
    name="lazy_web_search",
    description="Search the web for information using a controlled, truncated search engine.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."}
        },
        "required": ["query"],
    }
)
async def lazy_web_search(query: str, limit: int = 6, **_extra) -> str:
    """Keyless DuckDuckGo-lite search, executed HERE.

    This used to be a placeholder that returned "This tool is executed
    server-side via MCP." — but the gateway routes lazy_web_search straight
    back to this function over the python bridge, so every trading agent's
    web search returned that sentence instead of results. DDG's lite endpoint
    is static HTML with no bot wall and answers in about a second (same
    approach as HTML-Notes' html_notes_web_search).
    """
    import urllib.parse

    import httpx
    from bs4 import BeautifulSoup

    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
    limit = max(1, min(int(limit or 6), 10))
    resp = None
    last_err: Exception | None = None
    # One quick retry — DDG-lite intermittently stalls to the read timeout, and
    # a timeout's str() is EMPTY, which produced the useless "Search failed: "
    # rows in agent_tool_telemetry. Always report the exception type.
    for attempt, timeout_s in ((1, 20.0), (2, 10.0)):
        try:
            async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
                resp = await client.get(
                    "https://lite.duckduckgo.com/lite/",
                    params={"q": query},
                    headers={"User-Agent": ua},
                )
                resp.raise_for_status()
            break
        except Exception as e:
            last_err = e
            logger.warning(f"[WebTools] ddg lite search attempt {attempt} failed for "
                           f"{query!r}: {type(e).__name__}: {e}")
            resp = None
    if resp is None:
        return json.dumps({
            "status": "error",
            "message": f"Search failed: {type(last_err).__name__}: {last_err}",
        })

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for row in soup.find_all("tr"):
        if "sponsored" in " ".join(row.get("class") or []):
            continue
        link = row.find("a", class_="result-link")
        if link is not None:
            href = link.get("href") or ""
            if href.startswith("//"):
                href = "https:" + href
            # Organic links are wrapped as /l/?uddg=<percent-encoded target>.
            target = urllib.parse.parse_qs(
                urllib.parse.urlparse(href).query).get("uddg", [""])[0]
            results.append({"title": link.get_text(strip=True),
                            "url": target or href, "snippet": ""})
            continue
        cell = row.find("td", class_="result-snippet")
        if cell is not None and results:
            results[-1]["snippet"] = cell.get_text(strip=True)
    results = [r for r in results if r["url"]][:limit]
    if not results:
        return json.dumps({"status": "success", "results": [],
                           "message": "Search returned nothing. Retry with a shorter, simpler query."})
    return json.dumps({"status": "success", "results": results})
