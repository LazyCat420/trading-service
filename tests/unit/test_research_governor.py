"""
Research Governor Tests — anti-doom-loop guardrails for self-scheduled research.

schedule_research is now async and only creates `once` schedules sniped to a real
dated event (earnings auto-resolved, or an explicit ISO datetime). Coarse market
windows and `monitor` are retired (→ Watch Desk watch_ticker).
"""
import os
import sys
import json
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services import research_governor as gov


def _run(coro):
    return asyncio.run(coro)


def test_vague_reason_rejected():
    res = _run(gov.schedule_research(["AAPL"], when="", reason="check it"))
    assert res["status"] == "rejected"
    assert "reason is required" in res["reason"]


def test_coarse_window_rejected():
    # Coarse market windows are retired → redirect to watch_ticker.
    res = _run(gov.schedule_research(["AAPL"], when="next_open", reason="earnings follow-up on Q3 guidance"))
    assert res["status"] == "rejected"
    assert "retired" in res["reason"].lower()


def test_monitor_intent_rejected():
    res = _run(gov.schedule_research(
        ["AAPL"], when="", reason="keep monitoring this name", review_intent="monitor"))
    assert res["status"] == "rejected"
    assert "watch_ticker" in res["reason"]


def test_invalid_when_rejected():
    res = _run(gov.schedule_research(["AAPL"], when="someday", reason="earnings follow-up on Q3 guidance"))
    assert res["status"] == "rejected"
    assert "ISO-8601" in res["reason"]


def test_past_datetime_rejected():
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    res = _run(gov.schedule_research(["AAPL"], when=past, reason="earnings follow-up on Q3 guidance"))
    assert res["status"] == "rejected"
    assert "in the past" in res["reason"]


def test_far_future_rejected():
    future = (datetime.now(timezone.utc) + timedelta(days=gov.ONCE_MAX_DAYS + 5)).isoformat()
    res = _run(gov.schedule_research(["AAPL"], when=future, reason="earnings follow-up on Q3 guidance"))
    assert res["status"] == "rejected"
    assert "days out" in res["reason"]


def test_multi_ticker_auto_resolve_rejected():
    res = _run(gov.schedule_research(["AAPL", "MSFT"], when="", reason="earnings for both names soon"))
    assert res["status"] == "rejected"
    assert "single ticker" in res["reason"].lower()


def test_clean_tickers():
    out = gov._clean_tickers([" aapl", "AAPL", "msft ", "", None, 42])
    assert out == ["AAPL", "MSFT"]


def _mock_mongo(active_count=0, daily_count=0, queued_rows=None, recent_rows=None):
    """Patch mongo_query AND mongo_store on the module, dispatching on COLLECTION.

    Both are patched: patching only the reads would leave the governor's
    insert_docs() calls pointed at the real store.
    """
    q = MagicMock()
    store = MagicMock()

    def count(collection, query=None):
        query = query or {}
        if collection == "v3_system_commands":
            return 0
        if collection == "cycle_schedules":
            if "created_at" in query:
                return daily_count
            return active_count
        return 0

    def find_rows(collection, query, columns, sort=None, limit=0):
        if collection == "cycle_schedules":
            return queued_rows or []
        return []

    def group_rows(collection, query, keys, aggs, select, sort=None, limit=0):
        return []

    q.count.side_effect = count
    q.find_rows.side_effect = find_rows
    q.group_rows.side_effect = group_rows
    store.distinct_values.side_effect = lambda coll, field, query=None: [
        r[0] for r in (recent_rows or [])
    ]
    store.insert_docs.return_value = 1
    return q, store


def test_earnings_autoresolve_creates_once():
    # `when` omitted → governor resolves the ticker's earnings into a precise `once`.
    run_at = datetime.now(timezone.utc) + timedelta(days=6)
    q, store = _mock_mongo()
    with patch.object(gov, "mongo_query", q), patch.object(gov, "mongo_store", store), \
         patch("app.services.cycle_queue.mongo_store", store), \
         patch.object(gov, "_resolve_earnings_run_at", new=AsyncMock(return_value=run_at)):
        res = _run(gov.schedule_research(["NVDA"], when="", reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "scheduled"
    assert res["type"] == "once"
    written = {c.args[0]: c.args[1][0] for c in store.insert_docs.call_args_list}
    assert "cycle_schedules" in written
    assert written["cycle_schedules"]["schedule_type"] == "once"
    assert "v3_system_commands" in written


def test_no_earnings_found_rejected():
    q, store = _mock_mongo()
    with patch.object(gov, "mongo_query", q), patch.object(gov, "mongo_store", store), \
         patch.object(gov, "_resolve_earnings_run_at", new=AsyncMock(return_value=None)):
        res = _run(gov.schedule_research(["NVDA"], when="", reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "rejected"
    assert "No upcoming earnings" in res["reason"]


def _future_iso(days=5):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_active_cap_rejects():
    q, store = _mock_mongo(active_count=gov.MAX_ACTIVE_BOT_SCHEDULES)
    with patch.object(gov, "mongo_query", q), patch.object(gov, "mongo_store", store):
        res = _run(gov.schedule_research(["NVDA"], when=_future_iso(), reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "rejected"
    assert "already active" in res["reason"]


def test_dedupe_rejects_queued_ticker():
    q, store = _mock_mongo(queued_rows=[(json.dumps(["NVDA"]),)])
    with patch.object(gov, "mongo_query", q), patch.object(gov, "mongo_store", store):
        res = _run(gov.schedule_research(["NVDA"], when=_future_iso(), reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "rejected"
    assert "already queued" in res["reason"]


def test_cooldown_rejects_recent_ticker():
    q, store = _mock_mongo(recent_rows=[("NVDA",)])
    with patch.object(gov, "mongo_query", q), patch.object(gov, "mongo_store", store):
        res = _run(gov.schedule_research(["NVDA"], when=_future_iso(), reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "rejected"
    assert "Cooldown" in res["reason"]


def test_critical_bypasses_cooldown():
    q, store = _mock_mongo(recent_rows=[("NVDA",)])
    with patch.object(gov, "mongo_query", q), patch.object(gov, "mongo_store", store), \
         patch("app.services.cycle_queue.mongo_store", store):
        res = _run(gov.schedule_research(
            ["NVDA"], when=_future_iso(),
            reason="CEO resignation just hit the wire — reassess immediately", urgency="critical"))
    assert res["status"] == "scheduled"
