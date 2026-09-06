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
callers**, so the deferral never happened. That function, and the whole
deep-read path behind it, were deleted on 2026-08-10; this module is the only
body upgrade there is.

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
import contextlib
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
# How long we will WAIT FOR A SLOT before calling it starvation. Separate
# from the fetch budget on purpose: queue time is another pass's fetches.
UPGRADE_QUEUE_WAIT_S = float(os.getenv("NEWS_BODY_UPGRADE_QUEUE_WAIT_S", "30"))

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


# Upgrade passes must not race each other for the scraper pool.
#
# finnhub's pass and the rotator's pass run in the same cycle and both funnel
# into scraper_client's process-wide Semaphore(5). Whichever starts first holds
# every slot, and the other's wall-clock budget expires while its coroutines are
# still queued — so it returns nothing, having issued no request. Measured
# 2026-08-10: finnhub 97.9% upgraded, alphavantage and polygon 0% on every wide
# cycle. Waiting longer does not fix it; the winner can hold the pool for
# minutes. Taking turns does: each pass gets the full pool, and — the part that
# matters — its budget clock starts when it acquires the lock, not when it was
# queued behind someone else's fetches.
_UPGRADE_LOCKS: dict[object, asyncio.Lock] = {}


def _upgrade_lock() -> asyncio.Lock:
    """One lock per event loop, created lazily so it binds to the live loop."""
    loop = asyncio.get_running_loop()
    lock = _UPGRADE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _UPGRADE_LOCKS[loop] = lock
    return lock


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
    # The budget has to bound FETCHING, not QUEUEING. `_scrape_article_body_via_service`
    # funnels into scraper_client's process-wide Semaphore(5), which a concurrent
    # pass can hold for minutes — so a plain wall-clock budget expires while every
    # coroutine is still queued and the pass returns nothing. Measured 2026-08-10:
    # finnhub upgraded 97.9% while alphavantage/polygon got 0% on every wide cycle,
    # having never issued a request. `started` is set by the first coroutine that
    # actually acquires a slot; the fetch budget runs from there.
    # NOT set on acquiring `sem` — that one is ours and is never contended. The
    # queue that starves us is inside the scraper client, so the only honest
    # signal that we are being served is a body actually coming back.
    progress = asyncio.Event()
    failures = 0

    async def _one(url: str) -> None:
        nonlocal failures
        async with sem:
            try:
                body = await _scrape_article_body_via_service(url)
            except Exception as e:  # noqa: BLE001 — one URL must not stop the run
                failures += 1
                logger.debug("[body_upgrade] failed for %s: %s", url, e)
                return
            if not body:
                # `_scrape_article_body_via_service` signals failure by RETURNING
                # "" — it catches nothing and raises nothing. So the `except`
                # above could never fire, and the alarm line below printed
                # "0 fetch error(s)" through a total scraper outage while also
                # blaming the time budget it had not spent.
                failures += 1
                return
            if body and len(body) >= MIN_BODY_CHARS:
                bodies[url] = body
                _body_cache_put(url, body)
                progress.set()

    # Count only what THIS pass fetched. `bodies` already carries the cache
    # hits, so measuring against it reported "50 of 26 attempts" — a number
    # that cannot be read, in the one line that says whether this is working.
    before = len(bodies)
    starved = False
    queued_s = 0.0

    # Take turns. Waiting here is NOT charged to the fetch budget — that was the
    # whole defect.
    lock = _upgrade_lock()
    waited_from = time.monotonic()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=UPGRADE_QUEUE_WAIT_S)
    except asyncio.TimeoutError:
        queued_s = time.monotonic() - waited_from
        logger.warning(
            "[body_upgrade] waited %.0fs for another upgrade pass and gave up; "
            "%d article(s) keep their provider blurb",
            queued_s, len(attempted),
        )
        return bodies
    queued_s = time.monotonic() - waited_from

    task = asyncio.ensure_future(
        asyncio.gather(*(_one(u) for u in attempted), return_exceptions=True)
    )
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=UPGRADE_BUDGET_S)
    except asyncio.TimeoutError:
        starved = not progress.is_set()
        task.cancel()
    finally:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(task, return_exceptions=True)
        lock.release()

    fetched = len(bodies) - before
    # Report unconditionally. This line used to sit under `if fetched:`, so the
    # single outcome worth alarming on — nothing was upgraded — was the one that
    # printed nothing, and a whole source sat at 0% for a day reading as "no
    # candidates". A cap must log what it dropped or it reads as full coverage.
    if fetched == 0 and attempted:
        logger.warning(
            "[body_upgrade] upgraded 0 of %d articles (%s) — every one keeps its "
            "provider blurb. %d fetch error(s).",
            len(attempted),
            "starved: never acquired a scraper slot within "
            f"{UPGRADE_QUEUE_WAIT_S:.0f}s, another pass holds the pool"
            if starved else f"fetches exceeded the {UPGRADE_BUDGET_S:.0f}s budget",
            failures,
        )
    else:
        logger.info("[body_upgrade] %d of %d fetches returned an article%s",
                    fetched, len(attempted),
                    " (queue-starved for part of the run)" if starved else "")
    return bodies
