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

from app.db.connection import get_db
from app.collectors.yfinance_collector import collect_price_history

logger = logging.getLogger(__name__)

# yfinance is unauthenticated and will start refusing us if we hammer it.
# ~1.2s/ticker puts a 3.5k-ticker run at roughly 70 minutes.
PACE_SECONDS = 1.2

# A ticker that yfinance has no data for (delisted, renamed, acquired) is not a
# transient failure — congress data reaches back to 2012 and a lot of those
# symbols simply do not exist anymore. Retrying them every run wastes hours, so
# they get parked as 'empty' and skipped unless --retry-failed is passed.
TERMINAL_STATUSES = ("done", "empty")


def _ensure_progress_table():
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS price_backfill_progress (
                ticker       TEXT PRIMARY KEY,
                status       TEXT NOT NULL,
                rows_written INTEGER DEFAULT 0,
                error        TEXT,
                attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _load_universe(limit: int | None) -> list[str]:
    """Every ticker we need prices for, most-traded first.

    Ordering matters for a resumable job: if it dies halfway, the tickers that
    carry the most disclosure weight are already done.
    """
    with get_db() as db:
        rows = db.execute(
            """
            WITH universe AS (
                SELECT ticker, COUNT(*) AS weight
                FROM congress_trades
                WHERE ticker IS NOT NULL
                GROUP BY ticker
                UNION ALL
                SELECT ticker, COUNT(*) AS weight
                FROM sec_13f_holdings
                WHERE ticker IS NOT NULL
                GROUP BY ticker
            )
            SELECT ticker, SUM(weight) AS weight
            FROM universe
            -- Plain US equity symbols only. The disclosure feeds carry a little
            -- debris (option OCC symbols, foreign listings like MSTY.PA, 'N/A');
            -- yfinance can't price those and they'd just burn rate limit.
            WHERE ticker ~ '^[A-Z]{1,5}$'
            GROUP BY ticker
            ORDER BY weight DESC
            """
        ).fetchall()

    tickers = [r[0] for r in rows]
    return tickers[:limit] if limit else tickers


def _already_done(retry_failed: bool) -> set[str]:
    with get_db() as db:
        if retry_failed:
            rows = db.execute(
                "SELECT ticker FROM price_backfill_progress WHERE status = 'done'"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT ticker FROM price_backfill_progress WHERE status = ANY(%s)",
                (list(TERMINAL_STATUSES),),
            ).fetchall()
    return {r[0] for r in rows}


def _record(ticker: str, status: str, rows_written: int = 0, error: str | None = None):
    with get_db() as db:
        db.execute(
            """
            INSERT INTO price_backfill_progress (ticker, status, rows_written, error, attempted_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (ticker) DO UPDATE SET
                status       = EXCLUDED.status,
                rows_written = EXCLUDED.rows_written,
                error        = EXCLUDED.error,
                attempted_at = EXCLUDED.attempted_at
            """,
            (ticker, status, rows_written, error),
        )


async def backfill(limit: int | None = None, retry_failed: bool = False) -> dict:
    _ensure_progress_table()

    universe = _load_universe(limit)
    done = _already_done(retry_failed)
    todo = [t for t in universe if t not in done]

    logger.info(
        "[backfill] universe=%d already_done=%d todo=%d (~%.0f min)",
        len(universe), len(done), len(todo), len(todo) * PACE_SECONDS / 60,
    )

    stats = {"done": 0, "empty": 0, "failed": 0, "rows": 0}

    for i, ticker in enumerate(todo, 1):
        try:
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
    args = parser.parse_args()

    asyncio.run(backfill(limit=args.limit, retry_failed=args.retry_failed))
