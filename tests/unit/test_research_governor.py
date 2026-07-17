"""
Research Governor Tests — anti-doom-loop guardrails for self-scheduled research.

schedule_research is now async and only creates `once` schedules sniped to a real
dated event (earnings auto-resolved, or an explicit ISO datetime). Coarse market
windows and `monitor` are retired (→ Sentinel set_watch).
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
    # Coarse market windows are retired → redirect to set_watch.
    res = _run(gov.schedule_research(["AAPL"], when="next_open", reason="earnings follow-up on Q3 guidance"))
    assert res["status"] == "rejected"
    assert "retired" in res["reason"].lower()


def test_monitor_intent_rejected():
    res = _run(gov.schedule_research(
        ["AAPL"], when="", reason="keep monitoring this name", review_intent="monitor"))
    assert res["status"] == "rejected"
    assert "set_watch" in res["reason"]


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


def _mock_db(active_count=0, daily_count=0, queued_rows=None, recent_rows=None):
    """MagicMock get_db context yielding scripted query results."""
    db = MagicMock()

    def execute(sql, params=None):
        m = MagicMock()
        s = " ".join(sql.split())
        if "COUNT(*) FROM cycle_schedules WHERE id LIKE" in s and "is_active" in s:
            m.fetchone.return_value = [active_count]
        elif "COUNT(*) FROM cycle_schedules WHERE is_active = TRUE" in s:
            m.fetchone.return_value = [active_count]
        elif "COUNT(*) FROM cycle_schedules" in s and "24 hours" in s:
            m.fetchone.return_value = [daily_count]
        elif "COUNT(*) FROM v3_system_commands" in s:
            m.fetchone.return_value = [0]
        elif "SELECT tickers FROM cycle_schedules" in s:
            m.fetchall.return_value = queued_rows or []
        elif "SELECT payload FROM v3_system_commands" in s:
            m.fetchall.return_value = []
        elif "FROM analysis_results" in s:
            m.fetchall.return_value = recent_rows or []
        else:
            m.fetchall.return_value = []
            m.fetchone.return_value = [0]
        return m

    db.execute.side_effect = execute
    ctx = MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = False
    return ctx, db


def test_earnings_autoresolve_creates_once():
    # `when` omitted → governor resolves the ticker's earnings into a precise `once`.
    run_at = datetime.now(timezone.utc) + timedelta(days=6)
    ctx, db = _mock_db()
    with patch.object(gov, "get_db", return_value=ctx), \
         patch.object(gov, "_resolve_earnings_run_at", new=AsyncMock(return_value=run_at)):
        res = _run(gov.schedule_research(["NVDA"], when="", reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "scheduled"
    assert res["type"] == "once"
    sqls = [" ".join(c.args[0].split()) for c in db.execute.call_args_list]
    assert any("INSERT INTO cycle_schedules" in s for s in sqls)
    assert any("'once'" in s for s in sqls)
    assert any("INSERT INTO system_commands" in s for s in sqls)


def test_no_earnings_found_rejected():
    ctx, _ = _mock_db()
    with patch.object(gov, "get_db", return_value=ctx), \
         patch.object(gov, "_resolve_earnings_run_at", new=AsyncMock(return_value=None)):
        res = _run(gov.schedule_research(["NVDA"], when="", reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "rejected"
    assert "No upcoming earnings" in res["reason"]


def _future_iso(days=5):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_active_cap_rejects():
    ctx, _ = _mock_db(active_count=gov.MAX_ACTIVE_BOT_SCHEDULES)
    with patch.object(gov, "get_db", return_value=ctx):
        res = _run(gov.schedule_research(["NVDA"], when=_future_iso(), reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "rejected"
    assert "already active" in res["reason"]


def test_dedupe_rejects_queued_ticker():
    ctx, _ = _mock_db(queued_rows=[(json.dumps(["NVDA"]),)])
    with patch.object(gov, "get_db", return_value=ctx):
        res = _run(gov.schedule_research(["NVDA"], when=_future_iso(), reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "rejected"
    assert "already queued" in res["reason"]


def test_cooldown_rejects_recent_ticker():
    ctx, _ = _mock_db(recent_rows=[("NVDA",)])
    with patch.object(gov, "get_db", return_value=ctx):
        res = _run(gov.schedule_research(["NVDA"], when=_future_iso(), reason="Post-earnings drift check after Q2 beat"))
    assert res["status"] == "rejected"
    assert "Cooldown" in res["reason"]


def test_critical_bypasses_cooldown():
    ctx, _ = _mock_db(recent_rows=[("NVDA",)])
    with patch.object(gov, "get_db", return_value=ctx):
        res = _run(gov.schedule_research(
            ["NVDA"], when=_future_iso(),
            reason="CEO resignation just hit the wire — reassess immediately", urgency="critical"))
    assert res["status"] == "scheduled"
