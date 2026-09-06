"""The body upgrade starves behind a shared semaphore, and says nothing.

Two upgrade passes run in the same cycle — finnhub's, which now has thousands
of rows to fetch, and the rotator's for alphavantage/polygon. They do not share
`upgrade_bodies`' own semaphore (that one is created per call), but both funnel
into `scraper_client._get_semaphore("news")`, a process-wide `Semaphore(5)`.

So the rotator's 20s `asyncio.wait_for` budget is spent QUEUEING behind
finnhub's fetches rather than fetching. Measured in production on 2026-08-10:
finnhub 97.9% upgraded, alphavantage and polygon 0% on every multi-ticker
cycle, with not one of their candidate URLs appearing in any log — because
nothing was ever attempted.

The second failure is what made it invisible for a day: `upgrade_bodies` logs
its summary line under `if fetched:`, so **a pass that fetches nothing is the
one case that prints nothing at all**, and per-URL failures log at DEBUG.
"""
import asyncio
import logging

import pytest

from app.collectors import body_upgrade


@pytest.fixture(autouse=True)
def _fast_budget(monkeypatch):
    """Shrink the wall-clock budget so the test is quick but the shape is real."""
    monkeypatch.setattr(body_upgrade, "UPGRADE_BUDGET_S", 0.5)
    # Bound the queue allowance too, or a starved pass waits the full 30s and
    # the suite pays for it.
    monkeypatch.setattr(body_upgrade, "UPGRADE_QUEUE_WAIT_S", 3.0)
    monkeypatch.setattr(body_upgrade, "UPGRADE_LIMIT", 60)
    monkeypatch.setattr(body_upgrade, "UPGRADE_CONCURRENCY", 4)
    body_upgrade._BODY_CACHE.clear() if hasattr(body_upgrade, "_BODY_CACHE") else None


def _candidates(prefix: str, n: int):
    """n articles that all need an upgrade (blurb well under MIN_BODY_CHARS)."""
    return [(f"https://{prefix}.example/{i}", "short blurb") for i in range(n)]


def test_a_competing_pass_does_not_starve_this_one(monkeypatch):
    """The rotator must fetch SOMETHING while finnhub holds the shared pool.

    RED against current code: the shared Semaphore(5) is saturated by the
    competing pass, the 20s budget expires while every coroutine is still
    queued, and this returns {} having attempted nothing.
    """
    shared = asyncio.Semaphore(5)          # stands in for scraper_client's pool
    fetched_urls: list[str] = []

    async def fake_scrape(url: str, max_chars: int = 15000, **_kw) -> str:
        async with shared:                  # the contended resource
            await asyncio.sleep(0.05)
            fetched_urls.append(url)
            return "x" * 1500               # a real article

    monkeypatch.setattr(
        "app.collectors.news_collector._scrape_article_body_via_service", fake_scrape
    )

    async def scenario():
        # The competitor is what it is in production: ANOTHER upgrade_bodies
        # pass, big enough to outlast the other pass's budget. 400 fetches /
        # 5 shared slots * 0.05s = ~4s against a 0.5s budget. Modelling it as
        # raw scrape calls would let a fix that only serialises upgrade passes
        # look broken, and modelling it too small would let a broken fix look
        # fixed.
        finnhub = asyncio.create_task(
            body_upgrade.upgrade_bodies(_candidates("finnhub", 400))
        )
        await asyncio.sleep(0.01)           # let it claim the pool
        result = await body_upgrade.upgrade_bodies(_candidates("polygon", 8))
        finnhub.cancel()
        await asyncio.gather(finnhub, return_exceptions=True)
        return result

    result = asyncio.run(scenario())

    rotator_hits = [u for u in result if "polygon" in u]
    assert rotator_hits, (
        "the rotator pass fetched NOTHING while a competing pass held the shared "
        f"semaphore — this is the production 0% (fetched urls: "
        f"{len([u for u in fetched_urls if 'polygon' in u])} polygon of {len(fetched_urls)})"
    )


def test_a_pass_that_fetches_nothing_still_says_so(monkeypatch, caplog):
    """Zero fetched must produce a log line naming the loss.

    RED against current code: the summary line sits under `if fetched:`, so the
    single most important outcome — nothing was upgraded — is the one that
    prints nothing. A cap that does not log what it dropped reads as full
    coverage.
    """
    async def never_returns(url: str, max_chars: int = 15000, **_kw) -> str:
        await asyncio.sleep(30)             # outlives BOTH the budget and the queue allowance
        return "x" * 1500

    monkeypatch.setattr(
        "app.collectors.news_collector._scrape_article_body_via_service", never_returns
    )

    with caplog.at_level(logging.WARNING, logger="app.collectors.body_upgrade"):
        result = asyncio.run(body_upgrade.upgrade_bodies(_candidates("polygon", 6)))

    assert result == {}, "precondition: nothing should have been fetched"
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned, (
        "a pass that upgraded 0 of 6 articles logged nothing at WARNING — "
        "in production this read as 'no candidates', and the rotator's 0% went "
        "unnoticed for a day"
    )


def test_two_passes_take_turns_rather_than_interleaving(monkeypatch):
    """Upgrade passes must not overlap on the shared scraper pool.

    The starvation test above does NOT discriminate this: its queue allowance
    alone is enough to rescue a starved pass, so it stays green even with the
    turn-taking removed (verified by sabotage). This asserts the lock's actual
    contract instead — while one pass is fetching, no other pass is.

    That matters in production because finnhub's pass runs once per ticker; a
    pass that merely waits longer can still be lapped repeatedly, whereas one
    that takes a turn is guaranteed the whole pool and a full budget.
    """
    in_flight = {"finnhub": 0, "polygon": 0}
    overlaps: list[tuple[int, int]] = []

    async def fake_scrape(url: str, max_chars: int = 15000, **_kw) -> str:
        who = "finnhub" if "finnhub" in url else "polygon"
        in_flight[who] += 1
        if in_flight["finnhub"] and in_flight["polygon"]:
            overlaps.append((in_flight["finnhub"], in_flight["polygon"]))
        try:
            await asyncio.sleep(0.02)
            return "x" * 1500
        finally:
            in_flight[who] -= 1

    monkeypatch.setattr(
        "app.collectors.news_collector._scrape_article_body_via_service", fake_scrape
    )

    async def scenario():
        a = asyncio.create_task(body_upgrade.upgrade_bodies(_candidates("finnhub", 30)))
        b = asyncio.create_task(body_upgrade.upgrade_bodies(_candidates("polygon", 30)))
        return await asyncio.gather(a, b)

    asyncio.run(scenario())

    assert not overlaps, (
        f"two upgrade passes fetched concurrently ({len(overlaps)} overlapping "
        "moments) — they are competing for scraper_client's Semaphore(5) instead "
        "of taking turns, which is how one pass reached 97.9% while the other got 0%"
    )
