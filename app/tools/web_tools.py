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
        # Reachable at last. scraper_client.scrape used to return None on ANY
        # failure, so the `data.get("error")` half of this ternary was dead code
        # and the model was always told "Null response" — for a 410, for a
        # skipped domain, for a bot-wall alike. It now gets the scraper's own
        # reason and can act on it instead of concluding the service is broken.
        return json.dumps({
            "status": "error",
            "url": url,
            "message": data.get("error") or "Unknown scrape failure" if data else "Null response",
            "engine_used": data.get("engine_used") if data else None,
        })
    except Exception as e:
        logger.error(f"[WebTools] scrape_url error: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "url": url,
            "message": str(e)
        })

_SEARCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _normalize_title(title: str) -> str:
    """Lowercase alphanumeric skeleton, for cross-provider dedup."""
    import re
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _parse_rss_date(raw: str):
    """RFC-822 pubDate -> aware datetime, or None."""
    from email.utils import parsedate_to_datetime
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return None
    if dt is None:
        return None
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _apply_recency(results: list[dict], limit: int) -> list[dict]:
    """Newest first, and drop the archive.

    Both providers happily return decade-old material for a present-tense
    query: probed 2026-07-27, "Starbucks Q3 earnings catalyst" came back with a
    2012 earnings-call transcript, and "State Street institutional outflows"
    led with a March article. Unsorted and unlabelled, that is precisely the
    failure that put 2024 articles under a "Recent News" heading for ASC.

    Two-stage window rather than a hard cut: prefer the last 30 days, widen to
    a year only if that leaves too little to reason about, and always stamp
    age_days so the agent can weigh it. Undated results sort last but are kept
    — a missing pubDate is not evidence of age.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    for r in results:
        dt = _parse_rss_date(r.get("published", ""))
        r["_dt"] = dt
        r["age_days"] = (now - dt).days if dt else None

    dated = [r for r in results if r["_dt"] is not None]
    undated = [r for r in results if r["_dt"] is None]
    # Same-day ties go to a result the agent can actually scrape. Google News
    # is the fresher feed but returns no followable link, so a pure date sort
    # buries every Bing result the agent could pull the body from.
    dated.sort(key=lambda r: (r["_dt"].date(), bool(r.get("url"))), reverse=True)

    fresh = [r for r in dated if (r["age_days"] or 0) <= 30]
    if len(fresh) < 3:
        fresh = [r for r in dated if (r["age_days"] or 0) <= 365]

    ordered = fresh + undated
    for r in ordered:
        r.pop("_dt", None)
    return ordered[:limit]


async def _search_bing_news(client, query: str, limit: int) -> list[dict]:
    """Bing News RSS — the only probed endpoint that returns REAL article URLs.

    Links arrive wrapped as ``bing.com/news/apiclick.aspx?...&url=<target>``;
    the target is a plain query parameter, so no redirect hop is needed.
    """
    import urllib.parse
    import xml.etree.ElementTree as ET

    resp = await client.get(
        "https://www.bing.com/news/search",
        params={"q": query, "format": "RSS"},
        headers={"User-Agent": _SEARCH_UA},
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    out: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        target = urllib.parse.parse_qs(
            urllib.parse.urlparse(link).query).get("url", [""])[0]
        if not title or not target:
            continue
        out.append({
            "title": title,
            "url": target,
            "snippet": (item.findtext("description") or "").strip()[:400],
            "published": (item.findtext("pubDate") or "").strip(),
            "provider": "bing_news",
        })
        if len(out) >= limit:
            break
    return out


async def _search_gnews(client, query: str, limit: int) -> list[dict]:
    """Google News RSS — ~100 dated, publisher-attributed headlines per query.

    Its links are base64 ``news.google.com/rss/articles/CBMi...`` redirects.
    Probed 2026-07-27: a plain GET returns a 581 KB JavaScript interstitial
    still on news.google.com, so these URLs are NOT followable by scrape_url.
    They are returned with an empty ``url`` and a note rather than a dead link,
    so the agent does not burn a scrape on them. Headline + publisher + date is
    still enough to answer "is there a catalyst here?".
    """
    import xml.etree.ElementTree as ET

    resp = await client.get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
        headers={"User-Agent": _SEARCH_UA},
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    out: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "url": "",
            "snippet": "",
            "source": (item.findtext("source") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
            "provider": "google_news",
            "note": "headline only — Google News link is not directly followable",
        })
        if len(out) >= limit:
            break
    return out


@registry.register(
    name="lazy_web_search",
    description=(
        "Search recent news for a company, ticker or topic. Returns titles, "
        "publishers and dates; results carrying a 'url' can be passed to "
        "scrape_url for the full article text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."}
        },
        "required": ["query"],
    }
)
async def lazy_web_search(query: str, limit: int = 6, **_extra) -> str:
    """Keyless news search over a provider chain, executed HERE.

    History: this was DuckDuckGo-lite only. On 2026-07-27 DDG began refusing
    the NAS egress IP outright — both lite.duckduckgo.com and
    html.duckduckgo.com ConnectTimeout from inside the container while
    finnhub.io answers in 0.2s — so the junior analyst's ONLY research tool
    failed 8/8 in cycle-v3-1785137616 and 22% over the preceding week. Worse,
    the failure was silent: the analyst recorded "no qualitative catalysts
    found" and the triage gate skipped the Fundamental Analyst on that basis.

    Providers below were each probed from inside the deployed container.
    Bing News RSS is first because it is the only one that yields REAL article
    URLs. Bing's HTML endpoint is deliberately absent: it serves a JS shell to
    a browser UA, and to a text-browser UA it returns navigational hits
    (starbucks.com, "Menu") behind unresolvable ck/a wrappers.

    A total failure returns status "error" WITH ``degraded: true`` — callers
    must be able to tell "the web said nothing" from "we could not ask".
    """
    import httpx

    limit = max(1, min(int(limit or 6), 10))
    providers = (("bing_news", _search_bing_news), ("google_news", _search_gnews))

    results: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []

    # Over-fetch: _apply_recency discards the archive, and both providers mix
    # decade-old material into present-tense queries. Asking for exactly
    # `limit` would leave nothing after the age filter. Every provider is
    # queried — an early break on raw count would let a page of stale Bing
    # hits suppress fresher Google News headlines.
    fetch_n = min(limit * 4, 40)

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        for name, fn in providers:
            try:
                for r in await fn(client, query, fetch_n):
                    key = _normalize_title(r["title"])
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    results.append(r)
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}: {e}")
                logger.warning("[WebTools] search provider %s failed for %r: %s: %s",
                               name, query, type(e).__name__, e)

    if not results:
        # "No provider raised" and "no provider matched" are different facts
        # and used to share one message. Both RSS providers return HTTP 200
        # with zero <item>s for an over-specific query, so `errors` stays
        # empty and the tool reported an outage — 19 times in 14 days, on
        # queries like "TSMC TSM dividend history buyback share count trend
        # 2022 2023 2024 2025 2026". The agent was told the web was down and
        # reasonably retried something just as long.
        #
        # The distinction matters downstream too: the QUANT_ONLY triage gate
        # reads `degraded` to refuse to conclude "no catalyst" from a broken
        # tool. A genuinely empty result is evidence; an outage is not.
        if not errors:
            return json.dumps({
                "status": "empty",
                "degraded": False,
                "query": query,
                "message": (
                    f"No news matched. The query was {len(query.split())} words "
                    "long — RSS search matches headlines, not prose. Retry with "
                    "2-4 words: the company name plus one catalyst."
                ),
            })
        return json.dumps({
            "status": "error",
            "degraded": True,
            "message": "Web search unavailable — every provider failed: "
                       + "; ".join(errors),
        })

    final = _apply_recency(results, max(limit, 10))
    return json.dumps({
        "status": "success",
        "results": final,
        "followable": sum(1 for r in final if r.get("url")),
        "provider_errors": errors or None,
    })
