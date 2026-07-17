"""Regression: memory retrieval crashed with
'datetime.datetime' object has no attribute 'endswith' because psycopg returns
TIMESTAMPTZ columns as datetime objects, but the code called .endswith on them
(uncaught AttributeError). _coerce_dt must accept both datetime and str.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.memory.retriever import _coerce_dt, _is_stale


def test_coerce_dt_accepts_datetime():
    now = datetime.now(timezone.utc)
    assert _coerce_dt(now) == now


def test_coerce_dt_accepts_z_string():
    assert _coerce_dt("2026-07-17T22:00:00Z") is not None


def test_coerce_dt_naive_string_gets_utc():
    dt = _coerce_dt("2026-07-17T22:00:00")
    assert dt is not None and dt.tzinfo is not None


def test_coerce_dt_none_and_garbage():
    assert _coerce_dt(None) is None
    assert _coerce_dt("not a date") is None
    assert _coerce_dt(12345) is None


def test_is_stale_no_crash_on_datetime():
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert _is_stale({"updated_at": old}) is True
    assert _is_stale({"updated_at": datetime.now(timezone.utc)}) is False
