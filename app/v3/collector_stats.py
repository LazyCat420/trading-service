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


def record(cycle_id: str | None, ticker: str, ok: list[str], errored: list[str],
           timed_out: list[str], skipped: list[str]) -> None:
    if not cycle_id:
        return
    with _lock:
        agg = _stats.setdefault(cycle_id, {"ok": 0, "error": 0, "skipped": 0, "failures": []})
        agg["ok"] += len(ok)
        agg["error"] += len(errored) + len(timed_out)
        agg["skipped"] += len(skipped)
        agg["failures"].extend(
            [f"{ticker}:{name}:error" for name in errored]
            + [f"{ticker}:{name}:timeout" for name in timed_out]
        )


def consume(cycle_id: str | None) -> dict:
    """Return and clear the aggregate for a cycle (zeros if nothing recorded)."""
    with _lock:
        return _stats.pop(cycle_id or "", None) or {"ok": 0, "error": 0, "skipped": 0, "failures": []}
