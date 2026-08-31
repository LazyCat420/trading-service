"""The watchdog must diagnose the LIVE cycle, and must not halt on a dead table.

Two properties, both of which were broken on 2026-08-30 before the port:

1. Every read goes to MongoDB, by POSTGRES TABLE NAME. The three reads used to
   go through the ``scripts.migration`` archive pool, which still opens and
   still answers — from rows frozen at the 2026-08-19 cutover. Run against live
   Postgres minutes before the port, the watchdog said:

       Active Cycle ID: cycle-v3-1787179210 | Status: done | Phase: analyzing
       No active pipeline event crashes found. System healthy.

   while the live cycle (cycle-v3-1788074145) carried five error events, two of
   them ``v3_regime_engine CRASHED``. A silent wrong answer, not an exception.

2. The consecutive-failure halt is computed from the LIVE repair queue, never
   from ``pending_evolution_fixes``. That table was retired 2026-07-28 and
   froze at 96 rows on 2026-07-27; 6 of its 8 targets carry two ``rejected``
   rows on top, and four of those names — news, price_history, fundamentals,
   technicals — are live scrapers in ``target_map`` today. Reading it after the
   cutover is not a stale answer, it is a PERMANENT one: the halt returns
   before ``enqueue_job``, so logging the failure — the only thing this script
   still does — would never happen for those targets again, and no new row
   could ever clear it.

The fakes below record every collection name asked for, so a read that drifts
back to Postgres (no ``mongo_query`` calls at all) or to the retired table
fails here rather than in production.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def wd():
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import self_healing_watchdog as module

    module._last_handled_event = None
    return module


class FakeQuery:
    """Stands in for ``app.db.mongo_query`` and records what was asked for."""

    def __init__(self, rows_by_collection: dict[str, list[tuple]] | None = None):
        self._rows = rows_by_collection or {}
        self.calls: list[dict] = []

    def _record(self, fn, collection, query, columns, sort, limit):
        self.calls.append({"fn": fn, "collection": collection, "query": query,
                           "columns": list(columns), "sort": sort, "limit": limit})
        return list(self._rows.get(collection, []))

    def find_row(self, collection, query, columns, sort=None, **kw):
        rows = self._record("find_row", collection, query, columns, sort, 1)
        return rows[0] if rows else None

    def find_rows(self, collection, query, columns, sort=None, limit=0, **kw):
        rows = self._record("find_rows", collection, query, columns, sort, limit)
        return rows[:limit] if limit else rows

    @property
    def collections(self) -> list[str]:
        return [c["collection"] for c in self.calls]


# ── 1. the live pipeline_state singleton ────────────────────────────────────

def test_active_cycle_reads_the_live_pipeline_state_singleton(wd, monkeypatch):
    q = FakeQuery({"pipeline_state": [("cycle-v3-1788074145", "done", None, "analyzing")]})
    monkeypatch.setattr(wd, "mongo_query", q)

    assert wd.get_active_cycle() == ("cycle-v3-1788074145", "done", "", "analyzing")

    call = q.calls[0]
    # The POSTGRES table name, not a resolved collection: mongo_query resolves
    # it exactly once. Passing a physical name here resolves it twice the day
    # renames are switched on.
    assert call["collection"] == "pipeline_state"
    assert call["query"] == {"singleton_id": "current"}
    assert call["columns"] == ["cycle_id", "status", "error", "phase"]


def test_missing_singleton_still_returns_four_empty_strings(wd, monkeypatch):
    monkeypatch.setattr(wd, "mongo_query", FakeQuery({}))
    assert wd.get_active_cycle() == ("", "", "", "")


# ── 2. the error events of that cycle ───────────────────────────────────────

def test_error_events_read_pipeline_events_newest_first(wd, monkeypatch):
    q = FakeQuery({"pipeline_events": [
        ("analyzing", "v3_v3_regime_engine_crash_JPM", "💥 CRASHED", "T2"),
        ("trading", "trade_rejected", "SELL_NO_POSITION AAPL", "T1"),
    ]})
    monkeypatch.setattr(wd, "mongo_query", q)

    events = wd.get_latest_error_events("cycle-v3-1788074145")

    call = q.calls[0]
    assert call["collection"] == "pipeline_events"
    assert call["query"] == {"cycle_id": "cycle-v3-1788074145", "status": "error"}
    assert call["columns"] == ["phase", "step", "detail", "timestamp"]
    # Newest five BY TIMESTAMP. Without the sort, a limit takes natural order —
    # the OLDEST documents of a 201k-document growing collection.
    assert call["sort"] == [("timestamp", -1)]
    assert call["limit"] == 5
    # The benign policy-gate row is still dropped, after the fetch, as the SQL
    # version dropped it.
    assert [e["step"] for e in events] == ["v3_v3_regime_engine_crash_JPM"]


# ── 3. the halt guard reads the live queue, not the retired table ───────────

def test_consecutive_failures_reads_the_live_repair_queue(wd, monkeypatch):
    q = FakeQuery({"evolution_repair_queue": [("failed",), ("failed",)]})
    monkeypatch.setattr(wd, "mongo_query", q)

    assert wd.has_consecutive_failures("app/collectors/news_collector.py", "news") is True

    call = q.calls[0]
    assert call["collection"] == "evolution_repair_queue"
    assert call["query"] == {"target_path": "app/collectors/news_collector.py",
                             "target_symbol": "news"}
    assert call["sort"] == [("created_at", -1)] and call["limit"] == 2
    assert "pending_evolution_fixes" not in q.collections


def test_an_open_or_skipped_job_is_not_a_failure(wd, monkeypatch):
    # The live queue's real shape for app/collectors/yfinance_collector.py:
    # queued (2026-08-01) on top of skipped (2026-07-29). Nothing drains the
    # queue, so counting a queued row as a failure would halt every target that
    # was ever logged twice.
    monkeypatch.setattr(wd, "mongo_query",
                        FakeQuery({"evolution_repair_queue": [("queued",), ("skipped",)]}))
    assert wd.has_consecutive_failures("app/collectors/yfinance_collector.py",
                                       "yfinance") is False


def test_the_frozen_archive_cannot_halt_a_live_target(wd, monkeypatch):
    """The exact shape that made this a PERMANENT halt.

    ``pending_evolution_fixes`` still holds, for scraper/news, two ``rejected``
    rows dated 2026-05-21 and 2026-05-12 — the newest two, forever, because the
    table stopped taking writes on 2026-07-27. A read of it says "halt"; the
    live queue holds nothing for that target, so the correct answer is False
    and the failure gets logged.
    """
    q = FakeQuery({
        "pending_evolution_fixes": [("rejected",), ("rejected",)],
        "evolution_repair_queue": [],
    })
    monkeypatch.setattr(wd, "mongo_query", q)

    assert wd.has_consecutive_failures("app/collectors/news_collector.py", "news") is False
    assert q.collections == ["evolution_repair_queue"]


# ── 4. the guard and the write agree on WHICH target ────────────────────────

def test_the_guard_and_the_queue_key_on_the_same_target(wd, monkeypatch):
    """One key, resolved once.

    The guard used to be keyed on (target_type, target_name) — ('scraper',
    'news') — while ``enqueue_job`` keys on (target_path, target_symbol) —
    ('app/collectors/news_collector.py', 'news'). Two different questions about
    "this target", so the halt could never have been about the jobs the
    watchdog itself writes.
    """
    import app.cognition.evolution.coral.attempts as attempts

    guard_args: list[tuple] = []
    enqueued: list[dict] = []

    monkeypatch.setattr(wd, "get_active_cycle",
                        lambda: ("cycle-test", "running", "", "analyzing"))
    monkeypatch.setattr(wd, "get_latest_error_events", lambda cid: [{
        "phase": "collecting", "step": "news_fetch",
        "detail": "news collector exploded", "timestamp": "2026-08-30T07:21:55",
    }])
    monkeypatch.setattr(wd, "fetch_nas_cycle_logs", lambda cid: "")
    monkeypatch.setattr(wd, "detect_target_from_error", lambda msg: ("scraper", "news"))
    monkeypatch.setattr(wd, "resolve_target", lambda t, n: {
        "exists": True,
        "file_path": str(REPO_ROOT / "app/collectors/news_collector.py"),
        "relative_path": "app/collectors/news_collector.py",
    })
    monkeypatch.setattr(wd, "has_consecutive_failures",
                        lambda path, symbol: guard_args.append((path, symbol)) or False)
    monkeypatch.setattr(attempts, "enqueue_job",
                        lambda **kw: enqueued.append(kw) or "job-1234-5678")
    # Never touch reports/verified_fixes_history.md — it is a tracked file.
    monkeypatch.setattr(wd, "write_healing_report",
                        lambda *a, **k: None)

    asyncio.run(wd.heal_once())

    assert guard_args == [("app/collectors/news_collector.py", "news")]
    assert enqueued and (enqueued[0]["target_path"], enqueued[0]["target_symbol"]) \
        == guard_args[0]


# ── 5. no way back to Postgres ──────────────────────────────────────────────

def test_no_postgres_coupling_left_in_the_source(wd):
    import ast

    src = (REPO_ROOT / "scripts/self_healing_watchdog.py").read_text(encoding="utf-8")
    # The module docstring NAMES what was removed, on purpose — strip it and
    # grep the code, so the history stays writable and the coupling does not.
    doc = ast.get_docstring(ast.parse(src))
    code = src.replace(doc, "") if doc else src
    for needle in ("psycopg", "pg_connection", "DATABASE_URL", "get_db("):
        assert needle not in code, f"{needle} is back in the watchdog"
    assert not hasattr(wd, "get_db")
