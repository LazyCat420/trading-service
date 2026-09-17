"""Unit tests for Authoritative Policy Snapshot Service and Canonical Hashing."""

import datetime
import pytest
from app.trading.policy.snapshot_service import (
    build_policy_snapshot,
    canonical_json_dumps,
    compute_evaluation_key,
    compute_policy_config_hash,
    compute_snapshot_hash,
)


def test_canonical_json_deterministic():
    """Canonical JSON must recursively sort keys and format floats/dates predictably."""
    d1 = {"b": 2.123456, "a": 1, "nested": {"y": 20, "x": 10}}
    d2 = {"a": 1, "nested": {"x": 10, "y": 20}, "b": 2.123456}

    s1 = canonical_json_dumps(d1)
    s2 = canonical_json_dumps(d2)

    assert s1 == s2
    # Check float rounding to 4 decimals
    assert "2.1235" in s1
    # Check key ordering
    assert s1.startswith('{"a":1,"b":2.1235,"nested":{"x":10,"y":20}}')


def test_datetime_serialization_iso_utc():
    """Datetimes must serialize to ISO 8601 UTC with trailing 'Z'."""
    dt = datetime.datetime(2026, 9, 16, 22, 0, 0, tzinfo=datetime.timezone.utc)
    s = canonical_json_dumps({"time": dt})
    assert s == '{"time":"2026-09-16T22:00:00Z"}'


def test_snapshot_hash_perturbation_sensitivity():
    """Any state change must alter the snapshot_hash."""
    base = {
        "bot_id": "test-bot",
        "ticker": "AAPL",
        "cash_balance": 50000.0,
        "portfolio_equity": 100000.0,
        "quote_price": 150.00,
        "quote_age_hours": 0.5,
    }

    base_hash = compute_snapshot_hash(base)

    # 1. Perturb cash by $1
    p_cash = dict(base, cash_balance=50001.0)
    assert compute_snapshot_hash(p_cash) != base_hash

    # 2. Perturb quote by 1 cent
    p_quote = dict(base, quote_price=150.01)
    assert compute_snapshot_hash(p_quote) != base_hash

    # 3. Perturb quote age
    p_age = dict(base, quote_age_hours=0.6)
    assert compute_snapshot_hash(p_age) != base_hash


def test_evaluation_key_format():
    """Evaluation key must combine decision_id, revision, truncated hash, and policy version."""
    snap_hash = "abcdef0123456789fedcba9876543210"
    eval_key = compute_evaluation_key(
        decision_id="dec-12345",
        decision_revision=1,
        snapshot_hash=snap_hash,
        policy_version="v1.0",
    )
    assert eval_key == "dec-12345:v1:abcdef0123456789:v1.0"


def test_build_policy_snapshot_mocked(monkeypatch):
    """build_policy_snapshot computes mark-to-market equity and reservations."""
    def mock_find_row(coll, filt, cols, sort=None, session=None):
        if coll == "bots":
            return [40000.0, 100000.0]
        if coll == "price_history":
            return [300.0, datetime.datetime.now(datetime.timezone.utc), datetime.datetime.now(datetime.timezone.utc), "test", 300.0]
        return None

    monkeypatch.setattr("app.db.mongo_query.find_row", mock_find_row)
    # Mock positions: AAPL 100 shares @ $150 = $15,000; MSFT 100 shares @ $300 = $30,000
    monkeypatch.setattr(
        "app.db.mongo_query.find_rows",
        lambda coll, filt, cols, sort=None, session=None: (
            [["AAPL", 100.0, 150.0], ["MSFT", 100.0, 300.0]] if coll == "positions" else []
        ),
    )
    # Mock order capacity
    monkeypatch.setattr(
        "app.trading.order_capacity.pending_capacity",
        lambda bot_id, ticker: {"cash_reserved": 5000.0, "ticker_reserved": 0.0},
    )

    now = datetime.datetime(2026, 9, 16, 22, 0, 0, tzinfo=datetime.timezone.utc)
    snap_obj, snap_payload = build_policy_snapshot(
        bot_id="test-bot",
        ticker="AAPL",
        quote_price=160.0,  # Current quote for AAPL
        quote_age_hours=0.2,
        as_of=now,
    )

    # Cash = 40000, Positions = AAPL (100 * 160 = 16000) + MSFT (100 * 300 = 30000) = 46000.
    # Total Equity = 40000 + 46000 = 86000
    assert snap_obj.portfolio_equity == 86000.0
    assert snap_obj.cash_balance == 40000.0
    assert snap_payload["available_cash"] == 35000.0  # 40000 - 5000 reserved
    assert snap_obj.held_ticker_value == 16000.0
    assert snap_obj.is_held is True
    assert len(snap_payload["snapshot_hash"]) == 64
