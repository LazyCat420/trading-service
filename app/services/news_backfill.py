"""News fact-extraction backfill — the Jetson's standing job.

**Why this box, when the A/B said Gold Spark is the better extractor.** Because
the two jobs have different counterfactuals, and that is the whole argument:

  in-cycle  — an article an agent is about to read, uncached. Not extracting it
              means Gold Spark extracts it. Jetson vs Gold Spark: Gold Spark
              wins on yield (3.90 vs 3.00 grounded facts/article, n=40 twice),
              so it keeps that path.
  backfill  — an article nobody has asked for yet. Not extracting it means the
              agent gets RAW SCRAPE TEXT: ~2,300 chars leading with site
              navigation. Jetson vs nothing. There is no contest.

Measured 2026-08-07: 42,715 of 44,868 eligible articles (95.2%) had never been
extracted, because the in-cycle path is bounded by a 22-second per-cycle budget
that clears a handful of articles at a time and is outrun by ~1,000 newly
collected articles a day. That backlog is not a Gold Spark queue that grew too
long — Gold Spark is the contended box and stealing its capacity for articles
nobody has requested is exactly the trade this service should not make. It is
work that simply never had a machine.

The Jetson has done zero production work in its lifetime. This is the job:
low-priority, unbounded in patience, tolerant of a 77%-of-Gold-Spark yield
because the alternative is 0%, and running on a box whose idle capacity costs
nothing.

**Pinned hard, and that is deliberate.** If the Jetson is unreachable the
backfill stops. It must never fail over onto Gold Spark: silently converting
"the spare box is idle" into "the trading cycle's box is serving 42,000
low-priority extractions" is the failure mode this design exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable

from app.services.news_extraction import (
    ENABLED as _EXTRACTION_ENABLED,
    _MIN_TEXT_CHARS,
    _store_facts,
    extract_article_facts_with_source,
)

logger = logging.getLogger(__name__)

ENABLED = os.getenv("NEWS_BACKFILL_ENABLED", "true").lower() in ("1", "true", "yes")

# The box this job belongs to. A key, not a URL — resolution stays in one place.
ENDPOINT: tuple[str, ...] = tuple(
    k.strip() for k in os.getenv("NEWS_BACKFILL_ENDPOINT", "jetson").split(",")
    if k.strip()
)

# Articles per batch and how many run at once.
#
# **6, because the box's knee is 8 — measured, not guessed.** On 2026-08-07 the
# Jetson was load-tested by adding k concurrent streams ALONGSIDE this worker
# and reading total throughput from its own `/metrics`:
#
#   +0   2,554 req/hr   406 prefill tok/s   3 running   0 queued
#   +3   4,146 req/hr   751 prefill tok/s   6 running   0 queued
#   +6   5,325 req/hr   911 prefill tok/s   8 running   1 queued
#   +12  5,155 req/hr   904 prefill tok/s   8 running   7 queued   <- p50 doubles
#
# `num_requests_running` pins at exactly 8 and prefill throughput flatlines,
# so past 8 concurrent the box only queues. It is compute-bound, not
# memory-bound — `kv_cache_usage_perc` peaked at 12.7%, `num_preemptions_total`
# is 0, and `gpu_memory_utilization` is 0.7 — so raising `max_num_seqs` or
# handing vLLM more GPU memory would not move it.
#
# 6 is the largest setting measured with ZERO queueing, leaving 2 slots of
# headroom. Do not raise it to 8: the in-cycle extractor and the gatekeeper
# shadow also land on this box, and a queued shadow call times out into an
# `AGENT_ERROR` that is indistinguishable from the model failing.
_BATCH = int(os.getenv("NEWS_BACKFILL_BATCH", "24"))
_CONCURRENCY = int(os.getenv("NEWS_BACKFILL_CONCURRENCY", "6"))

# Stand down while a trading cycle runs — see `_cycle_is_running`.
YIELD_TO_CYCLE = os.getenv(
    "NEWS_BACKFILL_YIELD_TO_CYCLE", "true").lower() in ("1", "true", "yes")

# Pause between batches, and the longer nap taken when the backlog is empty.
#
# The batch pause was 20s against a ~35s batch — a 36% idle duty cycle, visible
# as the trailing zeros in a sampled `num_requests_running` trace:
#     0000233332323333333233332331312311110000
# At concurrency 6 a batch finishes in roughly half the time, so a 20s pause
# would have thrown away most of what the extra concurrency bought.
#
# Shortening it does NOT weaken the cycle stand-down. `_cycle_is_running` is
# checked once per batch, at the top, so what bounds an overrun is the in-flight
# BATCH, never this pause — measured 2026-08-07: the cycle began at 13:32:01.5
# and the last 15 straggler extractions all landed by 13:32:38, then nothing for
# 99 minutes. A shorter pause only makes the worker notice sooner, in both
# directions.
_IDLE_SLEEP_S = float(os.getenv("NEWS_BACKFILL_IDLE_SLEEP_S", "300"))
_BATCH_SLEEP_S = float(os.getenv("NEWS_BACKFILL_BATCH_SLEEP_S", "10"))

# Newest first. News relevance decays, so the marginal article worth extracting
# is the recent one an agent might still be asked about — not the oldest row in
# a 90-day backlog.
def _cycle_is_running() -> bool:
    """True while a trading cycle is live in MongoDB."""
    try:
        from app.db import mongo_store

        docs = mongo_store.find_docs(
            "pipeline_state",
            {},
            sort=[("updated_at", -1)],
            limit=1
        )
        return bool(docs) and str(docs[0].get("status", "")).lower() == "running"
    except Exception as e:  # noqa: BLE001
        logger.debug("[news-backfill] cycle check failed, proceeding: %s", e)
        return False


def _select_batch(limit: int) -> list[tuple[str, str, str, str]]:
    from app.db import mongo_store

    docs = mongo_store.find_docs(
        "news_articles",
        {"facts_extracted_at": None, "summary": {"$ne": None}},
        sort=[("collected_at", -1)],
        limit=limit,
    )
    return [
        (d.get("id", ""), d.get("ticker", ""), d.get("title") or "", d.get("summary", ""))
        for d in docs
        if len(d.get("summary") or "") >= _MIN_TEXT_CHARS
    ]


def backlog_size() -> int:
    """Eligible articles with no extraction attempt from MongoDB."""
    try:
        from app.db import mongo_store

        docs = mongo_store.find_docs(
            "news_articles",
            {"facts_extracted_at": None, "summary": {"$ne": None}},
        )
        return sum(1 for d in docs if len(d.get("summary") or "") >= _MIN_TEXT_CHARS)
    except Exception as e:  # noqa: BLE001
        logger.warning("[news-backfill] backlog count failed: %s", e)
        return 0


async def backfill_once(limit: int = _BATCH) -> dict[str, Any]:
    """Extract one batch on the pinned box. Returns counts; never raises.

    Every DB call goes through `asyncio.to_thread` because pymongo is a
    BLOCKING driver — a long-lived async loop doing sync I/O on the event loop
    stalls the health check, which reads as an unhealthy container rather than
    as a busy backfill. (This used to say "`get_db` is blocking psycopg". The
    driver changed at the 2026-08-19 cutover; the reason to_thread is here did
    not, and the client hit the same trap again on 2026-08-26 with /health
    measuring 11.2s.)
    """
    out = {"selected": 0, "extracted": 0, "facts": 0, "failed": 0, "yielded": False}
    if not (ENABLED and _EXTRACTION_ENABLED):
        return out

    if YIELD_TO_CYCLE and await asyncio.to_thread(_cycle_is_running):
        out["yielded"] = True
        return out

    rows = await asyncio.to_thread(_select_batch, limit)
    out["selected"] = len(rows)
    if not rows:
        return out

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(article_id: str, ticker: str, title: str, text: str) -> None:
        async with sem:
            facts, provider = await extract_article_facts_with_source(
                text, ticker, title, only=ENDPOINT)
            if facts is None:
                out["failed"] += 1
                return  # no store: an unextracted row is retried, a stored one isn't
            # "unknown", never "vllm": that string is the legacy constant this
            # change exists to distinguish itself from.
            await asyncio.to_thread(_store_facts, article_id, facts,
                                    provider or "unknown")
            out["extracted"] += 1
            out["facts"] += len(facts)

    await asyncio.gather(*(_one(*r) for r in rows))
    return out


async def backfill_loop(is_shutting_down: Callable[[], bool]) -> None:
    """Run batches forever, backing off when the backlog is empty or the box is
    down. Cancellation is a normal shutdown, not an error."""
    if not (ENABLED and _EXTRACTION_ENABLED):
        logger.info("[news-backfill] disabled (backfill=%s extraction=%s)",
                    ENABLED, _EXTRACTION_ENABLED)
        return

    logger.info("[news-backfill] starting on %s — backlog %d article(s)",
                ",".join(ENDPOINT), await asyncio.to_thread(backlog_size))
    consecutive_failures = 0
    try:
        while not is_shutting_down():
            t0 = time.monotonic()
            try:
                counts = await backfill_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a batch must not kill the loop
                logger.warning("[news-backfill] batch failed: %s", e)
                counts = {"selected": 0, "extracted": 0, "facts": 0, "failed": 1,
                          "yielded": False}

            if counts.get("yielded"):
                # Short nap, not the idle nap: a cycle is minutes long and the
                # worker should resume promptly once it ends, not sit out the
                # next five.
                await asyncio.sleep(_BATCH_SLEEP_S)
                continue

            if counts["selected"] == 0:
                await asyncio.sleep(_IDLE_SLEEP_S)
                continue

            logger.info(
                "[news-backfill] %d/%d extracted, %d facts, %d failed (%.1fs)",
                counts["extracted"], counts["selected"], counts["facts"],
                counts["failed"], time.monotonic() - t0,
            )

            # Every article in the batch failing means the box is gone, not that
            # the articles were bad — back off hard instead of spinning through
            # the backlog marking nothing done.
            if counts["extracted"] == 0:
                consecutive_failures += 1
                backoff = min(_IDLE_SLEEP_S, _BATCH_SLEEP_S * 2 ** consecutive_failures)
                logger.warning("[news-backfill] whole batch failed on %s; "
                               "backing off %.0fs", ",".join(ENDPOINT), backoff)
                await asyncio.sleep(backoff)
                continue

            consecutive_failures = 0
            await asyncio.sleep(_BATCH_SLEEP_S)
    except asyncio.CancelledError:
        logger.info("[news-backfill] cancelled")
        raise
