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

from app.db.connection import get_db
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

# Articles per batch and how many run at once. Concurrency 3 against a measured
# 6.5s median is ~28 articles/minute — comfortably ahead of the ~1,000/day
# inflow while leaving the box responsive for its shadow-comparison calls.
_BATCH = int(os.getenv("NEWS_BACKFILL_BATCH", "24"))
_CONCURRENCY = int(os.getenv("NEWS_BACKFILL_CONCURRENCY", "3"))

# Stand down while a trading cycle runs — see `_cycle_is_running`.
YIELD_TO_CYCLE = os.getenv(
    "NEWS_BACKFILL_YIELD_TO_CYCLE", "true").lower() in ("1", "true", "yes")

# Pause between batches, and the longer nap taken when the backlog is empty.
_IDLE_SLEEP_S = float(os.getenv("NEWS_BACKFILL_IDLE_SLEEP_S", "300"))
_BATCH_SLEEP_S = float(os.getenv("NEWS_BACKFILL_BATCH_SLEEP_S", "20"))

# Newest first. News relevance decays, so the marginal article worth extracting
# is the recent one an agent might still be asked about — not the oldest row in
# a 90-day backlog.
_SELECT_SQL = """
    SELECT id, ticker, COALESCE(title, ''), summary
    FROM news_articles
    WHERE facts_extracted_at IS NULL
      AND summary IS NOT NULL
      AND length(summary) >= %s
    ORDER BY collected_at DESC NULLS LAST
    LIMIT %s
"""


def _cycle_is_running() -> bool:
    """True while a trading cycle is live.

    The backfill yields to it, and not for CPU reasons — the two do not compete
    for a box, since the cycle extracts on Gold Spark. It is to protect a
    measurement: `MODEL_SHADOW_AGENTS` sends one gatekeeper prompt per cycle to
    this same Jetson, and that comparison is still accruing toward n>=10. A box
    kept permanently busy queues those calls into timeouts, and a timeout is
    recorded as `AGENT_ERROR` — indistinguishable, later, from the model having
    failed. The backlog has infinite patience; the shadow evidence does not.

    Fails OPEN (returns False) — an unreadable pipeline_state must not silently
    stop the worker forever.
    """
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT status FROM pipeline_state ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return bool(row) and str(row[0]).lower() == "running"
    except Exception as e:  # noqa: BLE001
        logger.debug("[news-backfill] cycle check failed, proceeding: %s", e)
        return False


def _select_batch(limit: int) -> list[tuple[str, str, str, str]]:
    with get_db() as db:
        rows = db.execute(_SELECT_SQL, [_MIN_TEXT_CHARS, limit]).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def backlog_size() -> int:
    """Eligible articles with no extraction attempt. Powers the log line that
    makes this job's progress visible; a worker whose output nobody can see
    reads as a worker that isn't running."""
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT count(*) FROM news_articles WHERE facts_extracted_at IS NULL "
                "AND summary IS NOT NULL AND length(summary) >= %s",
                [_MIN_TEXT_CHARS],
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        logger.warning("[news-backfill] backlog count failed: %s", e)
        return 0


async def backfill_once(limit: int = _BATCH) -> dict[str, Any]:
    """Extract one batch on the pinned box. Returns counts; never raises.

    Every DB call goes through `asyncio.to_thread` because `get_db` is blocking
    psycopg — a long-lived async loop doing sync I/O on the event loop stalls
    the health check, which reads as an unhealthy container rather than as a
    busy backfill.
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
