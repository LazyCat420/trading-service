"""Per-cycle pre-collection stats registry.

data_report.py records which collectors finished/failed/timed out for each
ticker; pipeline_service consumes the aggregate at cycle end so
cycle_run_summaries.collector_ok/collector_error/collector_skipped stop
reading 0 forever (they were never wired to anything).

In-memory only — stats live exactly as long as the cycle that produced them
and are dropped on consume(). Keyed by cycle_id so overlapping cycles can't
cross-contaminate.
"""

import threading

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


def consume(cycle_id: str | None) -> dict:
    """Return and clear the aggregate for a cycle (zeros if nothing recorded)."""
    with _lock:
        found = _stats.pop(cycle_id or "", None)
    if found:
        return found
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _EMPTY.items()}
