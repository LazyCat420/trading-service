"""Unit tests for MongoDB-ported scripts: confidence_audit.py and power_report.py."""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest

from scripts import confidence_audit, power_report


def test_no_postgres_imports_in_ported_scripts():
    """Ensure ported scripts do not import pg_connection or raw psycopg."""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for script_name in ("confidence_audit.py", "power_report.py"):
        path = os.path.join(base_dir, "scripts", script_name)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "pg_connection" not in content, f"{script_name} still contains pg_connection reference"
        assert "psycopg" not in content, f"{script_name} still contains psycopg reference"


def test_confidence_audit_prime():
    """Test _prime resolves forward returns using mongo_store."""
    confidence_audit._PX.clear()

    mock_find_docs = MagicMock()
    # First call: entry doc (date <= day)
    # Second call: exit docs (date > day)
    def side_effect(coll, query, **kwargs):
        if coll != "price_history":
            return []
        if "$lte" in query.get("date", {}):
            return [{"close": 100.0}]
        if "$gt" in query.get("date", {}):
            return [{"close": 105.0}, {"close": 110.0}]
        return []

    mock_find_docs.side_effect = side_effect

    with patch("app.quant.returns.dominant_source_for", return_value="polygon"), \
         patch("app.db.mongo_store.find_docs", mock_find_docs):
        rows = [("AAPL", "2026-06-01", "BUY", 80)]
        confidence_audit._prime(rows, horizon=2)

        key = ("AAPL", "2026-06-01", 2)
        assert key in confidence_audit._PX
        # (110 - 100) / 100 * 100 = 10.0%
        assert confidence_audit._PX[key] == pytest.approx(10.0)

        # Directional sign
        assert confidence_audit._signed("AAPL", "2026-06-01", 2, "BUY") == pytest.approx(10.0)
        assert confidence_audit._signed("AAPL", "2026-06-01", 2, "SELL") == pytest.approx(-10.0)


def test_confidence_audit_load_decisions():
    """Test load_decisions fetches from trade_results via mongo_query."""
    mock_find_rows = MagicMock(return_value=[("AAPL", "2026-06-01", "BUY", 75)])

    with patch("app.db.mongo_query.find_rows", mock_find_rows):
        res = confidence_audit.load_decisions("2026-06-01")
        assert len(res) == 1
        assert res[0] == ("AAPL", "2026-06-01", "BUY", 75)

        mock_find_rows.assert_called_once_with(
            "trade_results",
            {
                "created_at": {"$gte": "2026-06-01"},
                "action": {"$in": ["BUY", "SELL"]},
                "confidence": {"$ne": None},
            },
            ["ticker", "created_at", "action", "confidence"],
        )


def test_power_report_fetch():
    """Test power_report.fetch reads decision_outcomes and trade_fills from Mongo."""
    mock_find_rows = MagicMock(return_value=[
        (5.2, "2026-06-01", "BUY", "WIN"),
        (-2.1, "2026-06-02", "SELL", "WIN"),
    ])
    mock_count_docs = MagicMock(side_effect=[3, 42])  # 3 degraded, 42 fills

    with patch("app.db.mongo_query.find_rows", mock_find_rows), \
         patch("app.db.mongo_store.count_docs", mock_count_docs):
        rows, degraded, fills = power_report.fetch(include_degraded=False)

        assert len(rows) == 2
        assert rows[0]["pnl_pct"] == 5.2
        assert rows[1]["pnl_pct"] == -2.1
        assert degraded == 3
        assert fills == 42

        # Verify degraded filter was applied
        called_query = mock_find_rows.call_args[0][1]
        assert called_query["outcome"] == {"$ne": "DEGRADED_ARTIFACT"}


def test_confidence_audit_q2_synth():
    """Test q2_synth parses desk_data and partitions synth kept/cut."""
    desk_doc = {
        "final_decision": {"confidence": 85},
        "trade_decision": {"confidence": 65, "action": "BUY"},
    }
    mock_find_rows = MagicMock(return_value=[
        ("AAPL", "2026-06-01", desk_doc),
    ])

    with patch("app.db.mongo_query.find_rows", mock_find_rows), \
         patch.object(confidence_audit, "_prime", return_value=None), \
         patch.object(confidence_audit, "_signed", return_value=3.5):
        out = confidence_audit.q2_synth("2026-06-01", horizon=10)
        assert out["n_kept"] == 0
        assert out["n_cut"] == 1

