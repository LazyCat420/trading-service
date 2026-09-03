import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from app.services.pipeline_state import PipelineStateDB, _stringify_timestamp


def test_stringify_timestamp_attaches_utc():
    # Naive datetime
    naive = datetime(2026, 9, 2, 23, 43, 36, 596000)
    res = _stringify_timestamp(naive)
    assert res is not None
    assert res.endswith("+00:00") or res.endswith("Z")
    assert "2026-09-02T23:43:36.596000+00:00" in res

    # Aware datetime
    aware = datetime(2026, 9, 2, 23, 43, 36, 596000, tzinfo=timezone.utc)
    res_aware = _stringify_timestamp(aware)
    assert res_aware == "2026-09-02T23:43:36.596000+00:00"

    # None and empty
    assert _stringify_timestamp(None) is None
    assert _stringify_timestamp("") is None


def test_get_state_stringifies_dates_with_utc():
    sample_doc = {
        "singleton_id": "current",
        "cycle_id": "c_test_123",
        "status": "running",
        "started_at": datetime(2026, 9, 2, 23, 43, 36, 596000),  # naive from mongo
        "finished_at": None,
        "updated_at": datetime(2026, 9, 2, 23, 50, 0, 0),  # naive from mongo
    }

    with patch("app.services.pipeline_state.mongo_store.find_docs", return_value=[sample_doc]), \
         patch("app.services.pipeline_state.PipelineStateDB.get_cycle_events", return_value=[]), \
         patch("app.services.pipeline_state.PipelineStateDB.get_cycle_results", return_value=[]):
        state = PipelineStateDB.get_state()

        assert state["cycle_id"] == "c_test_123"
        assert state["started_at"] == "2026-09-02T23:43:36.596000+00:00"
        assert state["finished_at"] is None
        assert state["updated_at"] == "2026-09-02T23:50:00+00:00"
