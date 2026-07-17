"""
Research Governor Tests — anti-doom-loop guardrails for self-scheduled research.

Tests the pure-logic gates (no DB required):
  1. schedule_research rejects a vague/missing reason
  2. schedule_research rejects an invalid `when`
  3. schedule_research rejects a past datetime
  4. schedule_research rejects a datetime beyond the TTL horizon
  5. _clean_tickers normalizes and dedupes
  6. `when` window keywords map to policy schedules (DB mocked)
  7. governor caps reject when the active-schedule budget is spent (DB mocked)
"""
import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services import research_governor as gov


def test_vague_reason_rejected():
    res = gov.schedule_research(["AAPL"], "next_open", "check it")
    assert res["status"] == "rejected"
    assert "reason is required" in res["reason"]


def test_invalid_when_rejected():
    res = gov.schedule_research(["AAPL"], "someday", "earnings follow-up on Q3 guidance")
    assert res["status"] == "rejected"
    assert "must be one of" in res["reason"]


def test_past_datetime_rejected():
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    res = gov.schedule_research(["AAPL"], past, "earnings follow-up on Q3 guidance")
    assert res["status"] == "rejected"
    assert "in the past" in res["reason"]


def test_far_future_rejected():
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    res = gov.schedule_research(["AAPL"], future, "earnings follow-up on Q3 guidance")
    assert res["status"] == "rejected"
    assert "days out" in res["reason"]


def test_clean_tickers():
    # Dedupes, uppercases, and drops junk (None, "", non-strings)
    out = gov._clean_tickers([" aapl", "AAPL", "msft ", "", None, 42])
    assert out == ["AAPL", "MSFT"]


def _mock_db(active_count=0, daily_count=0, queued_rows=None, recent_rows=None):
    """Build a MagicMock get_db context yielding scripted query results."""
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


def test_window_creates_policy_schedule():
    ctx, db = _mock_db()
    with patch.object(gov, "get_db", return_value=ctx):
        res = gov.schedule_research(
            ["NVDA"], "next_pre_market", "Post-earnings drift check after Q2 beat"
        )
    assert res["status"] == "scheduled"
    assert res["type"] == "policy"
    assert res["fires"] == "next_pre_market"
    # Row + REFRESH_SCHEDULE command both written
    sqls = [" ".join(c.args[0].split()) for c in db.execute.call_args_list]
    assert any("INSERT INTO cycle_schedules" in s for s in sqls)
    assert any("INSERT INTO system_commands" in s for s in sqls)


def test_active_cap_rejects():
    ctx, _ = _mock_db(active_count=gov.MAX_ACTIVE_BOT_SCHEDULES)
    with patch.object(gov, "get_db", return_value=ctx):
        res = gov.schedule_research(
            ["NVDA"], "next_open", "Post-earnings drift check after Q2 beat"
        )
    assert res["status"] == "rejected"
    assert "already active" in res["reason"]


def test_dedupe_rejects_queued_ticker():
    ctx, _ = _mock_db(queued_rows=[(json.dumps(["NVDA"]),)])
    with patch.object(gov, "get_db", return_value=ctx):
        res = gov.schedule_research(
            ["NVDA"], "next_open", "Post-earnings drift check after Q2 beat"
        )
    assert res["status"] == "rejected"
    assert "already queued" in res["reason"]


def test_cooldown_rejects_recent_ticker():
    ctx, _ = _mock_db(recent_rows=[("NVDA",)])
    with patch.object(gov, "get_db", return_value=ctx):
        res = gov.schedule_research(
            ["NVDA"], "next_open", "Post-earnings drift check after Q2 beat"
        )
    assert res["status"] == "rejected"
    assert "Cooldown" in res["reason"]


def test_critical_bypasses_cooldown():
    ctx, _ = _mock_db(recent_rows=[("NVDA",)])
    with patch.object(gov, "get_db", return_value=ctx):
        res = gov.schedule_research(
            ["NVDA"], "next_open", "CEO resignation just hit the wire — reassess immediately",
            urgency="critical",
        )
    assert res["status"] == "scheduled"
