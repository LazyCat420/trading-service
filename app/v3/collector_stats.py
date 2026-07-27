"""Per-cycle pre-collection stats registry.

data_report.py records which collectors finished/failed/timed out for each
ticker; pipeline_service consumes the aggregate at cycle end so
cycle_run_summaries.collector_ok/collector_error/collector_skipped stop
reading 0 forever (they were never wired to anything).

In-memory only — stats live exactly as long as the cycle that produced them
and are dropped on consume(). Keyed by cycle_id so overlapping cycles can't
cross-contaminate.
"""

import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_stats: dict[str, dict] = {}   # cycle_id -> {"ok": int, "error": int, "skipped": int, "failures": [..]}


_EMPTY = {"ok": 0, "error": 0, "skipped": 0, "late": 0, "failures": [], "late_names": []}


def record(cycle_id: str | None, ticker: str, ok: list[str], errored: list[str],
           timed_out: list[str], skipped: list[str]) -> None:
    if not cycle_id:
        return
    with _lock:
        agg = _stats.setdefault(cycle_id, {k: (list(v) if isinstance(v, list) else v)
                                           for k, v in _EMPTY.items()})
        agg["ok"] += len(ok)
        agg["error"] += len(errored)
        agg["skipped"] += len(skipped)
        # "Timed out" collectors are NOT failures: data_report deliberately
        # leaves them running past the 45s report deadline, and their results
        # land in the DB for the next cycle (a 5-minute watchdog is the real
        # failure boundary). Folding them into `error` made every cold-ticker
        # cycle read as "15/15 collectors failed" when nothing had failed —
        # which is exactly how a healthy cycle got audited as broken.
        agg["late"] += len(timed_out)
        agg["failures"].extend(f"{ticker}:{name}:error" for name in errored)
        agg["late_names"].extend(f"{ticker}:{name}" for name in timed_out)

    _record_source_status(ticker, ok=ok, errored=errored, timed_out=timed_out)


def _record_source_status(ticker: str, ok: list[str], errored: list[str],
                          timed_out: list[str]) -> None:
    """Mirror per-collector outcomes into `data_source_status`.

    That table is the one an operator reads to ask "is this feed alive?", and
    it had exactly ONE writer: fred_collector. Measured 2026-07-27, 12 of its
    13 sources were frozen at 2026-06-24 while every one of them was in fact
    collecting normally — a health table reporting a month-long outage that
    was not happening is worse than no health table, because it trains you to
    ignore it.

    Best-effort by construction: a status-bookkeeping failure must never
    affect the collection it is describing.
    """
    try:
        from app.db.connection import get_db

        rows = ([(n, True) for n in ok]
                + [(n, False) for n in errored]
                + [(n, False) for n in timed_out])
        if not rows:
            return
        with get_db() as db:
            for name, succeeded in rows:
                if succeeded:
                    db.execute(
                        """
                        INSERT INTO data_source_status (source, ticker, last_success, error_msg)
                        VALUES (%s, %s, NOW(), NULL)
                        ON CONFLICT (source, ticker)
                        DO UPDATE SET last_success = NOW(), error_msg = NULL
                        """,
                        [name, ticker],
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO data_source_status (source, ticker, last_failure, error_msg)
                        VALUES (%s, %s, NOW(), %s)
                        ON CONFLICT (source, ticker)
                        DO UPDATE SET last_failure = NOW(),
                                      error_msg = EXCLUDED.error_msg
                        """,
                        [name, ticker, "no data or past pre-collect deadline"],
                    )
    except Exception:  # pragma: no cover - bookkeeping must never break collection
        logger.debug("[collector_stats] data_source_status update failed for %s",
                     ticker, exc_info=True)


def consume(cycle_id: str | None) -> dict:
    """Return and clear the aggregate for a cycle (zeros if nothing recorded)."""
    with _lock:
        found = _stats.pop(cycle_id or "", None)
    if found:
        return found
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _EMPTY.items()}
