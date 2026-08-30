"""
Backfill long-horizon daily OHLCV into price_history.

Rationale: 13F and congressional-disclosure return math needs prices going back
as far as the disclosures do (congress_trades starts 2012). Historical OHLCV is
immutable, so this is a one-time cost that never needs re-paying — we fetch
period="max" once and keep it.

The ticker universe is every symbol that appears in a disclosure we score:
congress_trades UNION sec_13f_holdings, plus the watchlist.

Resumable by design: progress is journalled to price_backfill_progress, and a
ticker already covered back to its first disclosure is skipped. Safe to re-run
after a crash, a deploy, or a rate-limit wall.

Usage:
    python -m scripts.backfill_price_history               # full universe
    python -m scripts.backfill_price_history --limit 100   # smoke test
    python -m scripts.backfill_price_history --retry-failed
"""

import argparse
import asyncio
import logging
import time

from datetime import datetime, timezone

from app.db import mongo_store
from app.collectors.explicit_fetch_guard import is_blocked_for_explicit_fetch
from app.collectors.yfinance_collector import collect_price_history

logger = logging.getLogger(__name__)

# yfinance is unauthenticated and will start refusing us if we hammer it.
# ~1.2s/ticker puts a 3.5k-ticker run at roughly 70 minutes.
PACE_SECONDS = 1.2

# A ticker that yfinance has no data for (delisted, renamed, acquired) is not a
# transient failure — congress data reaches back to 2012 and a lot of those
# symbols simply do not exist anymore. Retrying them every run wastes hours, so
# they get parked as 'empty' and skipped unless --retry-failed is passed.
#
# 'blocked' is NOT terminal in the same sense and is recorded separately
# (2026-08-08). `collect_price_history` returns 0 both when the vendor has
# nothing and when the guard refuses to ask, and this script wrote "no data
# returned" for both. 50 real companies — Agilent, Allstate, AppLovin,
# Alexandria Real Estate, DuPont — were parked as *absent* when they had in
# fact been *refused*, and 'empty' being terminal meant no later run would ever
# find out. A refusal recorded as a result is the failure shape this codebase
# keeps paying for; the journal must say which one happened.
TERMINAL_STATUSES = ("done", "empty", "blocked")


def _ensure_progress_table():
    """Mongo creates the collection on first write, so only the key needs
    declaring — and it has to be declared, because `ticker` was the PRIMARY KEY
    and _record below relies on it to upsert rather than duplicate."""
    mongo_store.get_doc_db()["price_backfill_progress"].create_index(
        "ticker", unique=True
    )


def _load_universe(limit: int | None) -> list[str]:
    """Every ticker we need prices for, most-traded first.

    Ordering matters for a resumable job: if it dies halfway, the tickers that
    carry the most disclosure weight are already done.
    """
    # Plain US equity symbols only. The disclosure feeds carry a little debris
    # (option OCC symbols, foreign listings like MSTY.PA, 'N/A'); yfinance
    # can't price those and they'd just burn rate limit.
    plain_symbol = {"ticker": {"$regex": "^[A-Z]{1,5}$"}}
    # UNION ALL of two per-collection counts -> $unionWith, so this stays one
    # round-trip instead of two scans stitched together in Python.
    rows = mongo_store.aggregate("congress_trades", [
        {"$match": {"ticker": {"$nin": [None]}, **plain_symbol}},
        {"$group": {"_id": "$ticker", "weight": {"$sum": 1}}},
        {"$unionWith": {"coll": "sec_13f_holdings", "pipeline": [
            {"$match": {"ticker": {"$nin": [None]}, **plain_symbol}},
            {"$group": {"_id": "$ticker", "weight": {"$sum": 1}}},
        ]}},
        {"$group": {"_id": "$_id", "weight": {"$sum": "$weight"}}},
        {"$sort": {"weight": -1}},
    ])

    tickers = [r["_id"] for r in rows]
    return tickers[:limit] if limit else tickers


def _already_done(retry_failed: bool) -> set[str]:
    # status = ANY(list) -> $in. retry_failed narrows "already done" to the
    # successes so the failures get another attempt.
    q = ({"status": "done"} if retry_failed
         else {"status": {"$in": list(TERMINAL_STATUSES)}})
    return set(mongo_store.distinct_values("price_backfill_progress", "ticker", q))


def _record(ticker: str, status: str, rows_written: int = 0, error: str | None = None):
    mongo_store.upsert_doc(
        "price_backfill_progress", {"ticker": ticker},
        {"ticker": ticker, "status": status, "rows_written": rows_written,
         "error": error, "attempted_at": datetime.now(timezone.utc)},
    )


async def backfill(limit: int | None = None, retry_failed: bool = False,
                   tickers: list[str] | None = None) -> dict:
    _ensure_progress_table()

    if tickers:
        # An explicit list bypasses BOTH the universe query and the progress
        # journal. The caller has named these, and the journal is exactly what
        # a targeted run is usually there to correct — a symbol wrongly parked
        # as 'empty' can only be reached by ignoring the parking.
        universe = [t.upper().strip() for t in tickers if t.strip()]
        done: set[str] = set()
        todo = universe
        logger.info("[backfill] explicit list of %d ticker(s) — journal ignored",
                    len(todo))
    else:
        universe = _load_universe(limit)
        done = _already_done(retry_failed)
        todo = [t for t in universe if t not in done]

    logger.info(
        "[backfill] universe=%d already_done=%d todo=%d (~%.0f min)",
        len(universe), len(done), len(todo), len(todo) * PACE_SECONDS / 60,
    )

    stats = {"done": 0, "empty": 0, "blocked": 0, "failed": 0, "rows": 0}

    for i, ticker in enumerate(todo, 1):
        try:
            # Ask the guard BEFORE the fetch, so a refusal is journalled as a
            # refusal. `collect_price_history` returns 0 for both, and reading
            # that as "no data returned" is what parked 50 real companies as
            # absent for good.
            if is_blocked_for_explicit_fetch(ticker):
                _record(ticker, "blocked", 0, "refused by the explicit-fetch guard")
                stats["blocked"] += 1
                continue

            # period="max" — grab the entire history in one request. Refetching
            # narrower windows later would cost another full pass over the
            # universe for data that never changes.
            rows = await collect_price_history(ticker, period="max")
            if rows > 0:
                _record(ticker, "done", rows)
                stats["done"] += 1
                stats["rows"] += rows
            else:
                _record(ticker, "empty", 0, "no data returned")
                stats["empty"] += 1
        except Exception as e:
            _record(ticker, "failed", 0, str(e)[:500])
            stats["failed"] += 1
            logger.warning("[backfill] %s failed: %s", ticker, e)

        if i % 50 == 0:
            logger.info(
                "[backfill] %d/%d — done=%d empty=%d failed=%d rows=%d",
                i, len(todo), stats["done"], stats["empty"], stats["failed"], stats["rows"],
            )

        time.sleep(PACE_SECONDS)

    logger.info("[backfill] COMPLETE — %s", stats)
    return stats


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--tickers",
        help="comma-separated symbols to backfill, ignoring the progress "
             "journal. Use this to repair symbols wrongly parked as 'empty'.",
    )
    args = parser.parse_args()

    asyncio.run(backfill(
        limit=args.limit,
        retry_failed=args.retry_failed,
        tickers=args.tickers.split(",") if args.tickers else None,
    ))
