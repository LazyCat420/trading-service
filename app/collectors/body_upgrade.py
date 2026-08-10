"""
body_upgrade.py — fetch the article behind a provider's blurb.
---------------------------------------------------------------
Shared by the news collectors, because the same hole was in each of them: the
API returns a 300-500 char summary, an article needs ~900 to be worth reading,
and nothing asked for the rest. Measured over 30 days before this existed:

    source          rows   under 900   avg chars   upgrade path
    finnhub       11,102        98%          303   none at all
    alphavantage   4,658        99%          471   trigger never fired
    polygon        3,060       100%          457   trigger never fired

`news_api_rotator` only scraped when the summary was under **150** chars, and
providers return more than that, so it never fired. `news_collector`'s finnhub
path removed body scraping outright "to fix 120s timeouts" and left a comment
deferring the work to `deep_read_top_articles()` — a function with **no
callers**, so the deferral never happened.

Those 120s timeouts were real, and this is the bounded form of what caused
them. Every caller gets the same three bounds and the same cross-ticker cache,
so there is one place to tune when they turn out to be wrong:

  * a cap on how many articles are attempted (shortest summary first, since
    that is where the most is gained),
  * a concurrency limit, so the scraper's per-domain rate limiter is respected
    rather than fought,
  * a wall-clock budget, after which whatever finished is used.

Run this BEFORE opening a database transaction. The original did the scraping
inside one, pinning a connection for the length of ~100 sequential fetches.
"""

import asyncio
import logging
import os
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

# The bar an article has to clear to be worth storing as one. Shared with the
# scraper's own thin-content gate so the trigger and the gate cannot drift
# apart: asking for a body the scraper would then refuse is pure waste, and
# the two drifting is exactly how the 150/900 band opened up.
try:
    from app.scraper.core.content_quality import MIN_ARTICLE_CHARS as MIN_BODY_CHARS
except Exception:  # noqa: BLE001 — keep the collectors importable standalone
    MIN_BODY_CHARS = 900

# Sized to cover a whole run rather than a fraction of it. Measured on the
# deployed container: a SINGLE-ticker rotator call produced 56 candidates (the
# providers are queried on a general query, not per ticker), so a cap of 25
# left 31 articles on their blurb every time — and shortest-first meant the
# skipped ones were exactly the 400-500 band that is the bulk of the problem.
UPGRADE_LIMIT = int(os.getenv("NEWS_BODY_UPGRADE_LIMIT", "60"))
UPGRADE_CONCURRENCY = int(os.getenv("NEWS_BODY_UPGRADE_CONCURRENCY", "4"))

# The real bound, because collection runs once per ticker inside the data
# phase. Measured against a no-upgrade control on the container: collection
# takes 28.5-36.2s on its own and the upgrade adds ~4-6s, so this is a safety
# ceiling rather than the usual cost. Nothing is permanently lost when it
# binds: articles are shared across tickers and deduplicated in the DB, so
# what one ticker runs out of time for is picked up by the next — increasingly
# from the cache below rather than the network.
UPGRADE_BUDGET_S = float(os.getenv("NEWS_BODY_UPGRADE_BUDGET_S", "20"))

# Collection runs ONCE PER TICKER and each ticker sees largely the same
# articles, so without a memory an 80-ticker cycle re-scrapes the same ~56 URLs
# 80 times. Successes only — caching a miss would suppress the retry that fixes
# it. Bounded and short-lived, because a stale body is worse than a re-fetch.
# Dead URLs are already remembered by the scraper's own failure cache; this is
# the other half of the same idea.
_BODY_CACHE_TTL_S = float(os.getenv("NEWS_BODY_CACHE_TTL_S", "3600"))
_BODY_CACHE_MAX = int(os.getenv("NEWS_BODY_CACHE_MAX", "512"))
_BODY_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()


def _body_cache_get(url: str) -> str | None:
    hit = _BODY_CACHE.get(url)
    if hit is None:
        return None
    expires_at, body = hit
    if time.time() >= expires_at:
        del _BODY_CACHE[url]
        return None
    _BODY_CACHE.move_to_end(url)
    return body


def _body_cache_put(url: str, body: str) -> None:
    _BODY_CACHE[url] = (time.time() + _BODY_CACHE_TTL_S, body)
    _BODY_CACHE.move_to_end(url)
    while len(_BODY_CACHE) > _BODY_CACHE_MAX:
        _BODY_CACHE.popitem(last=False)


def needs_upgrade(summary: str | None) -> bool:
    """Is this summary a blurb rather than an article?"""
    s = summary or ""
    return len(s) < MIN_BODY_CHARS or "..." in s


async def upgrade_bodies(candidates: list[tuple[str | None, str | None]]) -> dict[str, str]:
    """Fetch real bodies for ``(url, summary)`` pairs that need one.

    Returns ``{url: body}`` for whatever was obtained — cache hits included.
    A URL absent from the result simply keeps its provider blurb; an upgrade
    that fails must never leave an article worse off than not attempting one.
    """
    from app.collectors.news_collector import _scrape_article_body_via_service

    if not UPGRADE_LIMIT:
        return {}

    # One attempt per URL even when several articles share it.
    needed: dict[str, int] = {}
    bodies: dict[str, str] = {}
    cached = 0
    for url, summary in candidates:
        if not url or not needs_upgrade(summary):
            continue
        hit = _body_cache_get(url)          # fetched for an earlier ticker
        if hit is not None:
            bodies[url] = hit
            cached += 1
            continue
        n = len(summary or "")
        needed[url] = min(needed.get(url, n), n)

    if cached:
        logger.info("[body_upgrade] %d served from cache", cached)
    if not needed:
        return bodies

    # Shortest summary first — the blurbs with the least in them already.
    ordered = sorted(needed, key=lambda u: needed[u])
    attempted, dropped = ordered[:UPGRADE_LIMIT], ordered[UPGRADE_LIMIT:]
    if dropped:
        logger.info(
            "[body_upgrade] attempting %d of %d (cap=%d); %d left with their blurb",
            len(attempted), len(needed), UPGRADE_LIMIT, len(dropped),
        )

    sem = asyncio.Semaphore(UPGRADE_CONCURRENCY)

    async def _one(url: str) -> None:
        async with sem:
            try:
                body = await _scrape_article_body_via_service(url)
            except Exception as e:  # noqa: BLE001 — one URL must not stop the run
                logger.debug("[body_upgrade] failed for %s: %s", url, e)
                return
            if body and len(body) >= MIN_BODY_CHARS:
                bodies[url] = body
                _body_cache_put(url, body)

    # Count only what THIS pass fetched. `bodies` already carries the cache
    # hits, so measuring against it reported "50 of 26 attempts" — a number
    # that cannot be read, in the one line that says whether this is working.
    before = len(bodies)
    try:
        await asyncio.wait_for(
            asyncio.gather(*(_one(u) for u in attempted), return_exceptions=True),
            timeout=UPGRADE_BUDGET_S,
        )
    except asyncio.TimeoutError:
        logger.info(
            "[body_upgrade] hit its %.0fs budget — %d of %d fetched, "
            "the rest keep their blurb",
            UPGRADE_BUDGET_S, len(bodies) - before, len(attempted),
        )

    fetched = len(bodies) - before
    if fetched:
        logger.info("[body_upgrade] %d of %d fetches returned an article",
                    fetched, len(attempted))
    return bodies
