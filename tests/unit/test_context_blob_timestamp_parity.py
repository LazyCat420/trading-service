"""context_blobs must write ONE timestamp to both stores.

PG used to take `created_at` from the column default (CURRENT_TIMESTAMP, which
is transaction-start time) while the Mongo mirror took a Python
`datetime.now()` computed moments earlier. Two clocks for one fact. They agreed
whenever both landed in the same millisecond, so counts and sampled field
verification both scored it OK — an exhaustive sweep found 117 of 56,452 rows
drifted, Mongo always the earlier, by 1.0-6.3 ms.

These tests assert the values are identical BY CONSTRUCTION: the same object
reaches the SQL parameters and the mirrored document.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services import rlm_audit


class _Recorder:
    """Stands in for the pooled cursor, capturing every statement + params."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return [1]

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_audit(recorder, mirrored):
    """Drive log_rlm_audit_trail with PG captured and the Mongo mirror ON."""
    fake_store = MagicMock()
    fake_store.writes_mongo.return_value = True
    fake_store.upsert_doc.side_effect = lambda table, key, rec, **kw: mirrored.append((table, rec))

    with patch.object(rlm_audit, "get_db", return_value=recorder), \
         patch.dict("sys.modules", {"app.db.mongo_store": fake_store}):
        rlm_audit.log_rlm_audit_trail(
            cycle_id="cycle-test", bot_id="bot-test", ticker="AAPL",
            context="ctx", trading_system_prompt="sys", active_model="m",
            response_text="resp", tokens_used=1, execution_time=0.5,
        )


def _blob_inserts(recorder):
    return [(sql, p) for sql, p in recorder.calls if "INSERT INTO context_blobs" in sql]


def test_pg_insert_supplies_created_at_explicitly():
    """If PG is left to its column default, the two stores cannot agree."""
    rec, mirrored = _Recorder(), []
    _run_audit(rec, mirrored)

    inserts = _blob_inserts(rec)
    assert inserts, "no context_blobs INSERT was issued — the test proved nothing"
    for sql, params in inserts:
        assert "created_at" in sql, (
            "context_blobs INSERT does not name created_at, so PG falls back to "
            "CURRENT_TIMESTAMP while Mongo carries a Python clock"
        )
        assert any(isinstance(p, datetime) for p in params), \
            "no datetime was bound to the INSERT"


def test_both_stores_receive_the_identical_timestamp():
    """The real assertion: same value, not merely both present."""
    rec, mirrored = _Recorder(), []
    _run_audit(rec, mirrored)

    inserts = _blob_inserts(rec)
    assert inserts, "no context_blobs INSERT was issued — the test proved nothing"
    assert mirrored, "no Mongo mirror was written — the test proved nothing"

    pg_times = [p for _sql, params in inserts for p in params if isinstance(p, datetime)]
    mongo_times = [rec_["created_at"] for _t, rec_ in mirrored]
    assert pg_times and mongo_times

    # Every blob in one call shares one timestamp, and PG's equals Mongo's.
    assert len(set(pg_times)) == 1, f"PG got {len(set(pg_times))} distinct timestamps"
    assert set(pg_times) == set(mongo_times), (
        f"PG wrote {sorted(set(pg_times))} but Mongo wrote {sorted(set(mongo_times))} — "
        "two clocks for one fact"
    )


def test_timestamp_is_naive_utc_matching_the_column_type():
    """`created_at` is `timestamp without time zone`; pymongo reads naive as UTC.

    A tz-aware value would be adapted as timestamptz and cast by the session
    TimeZone, which is how a whole-hour shift gets into a column silently.
    """
    rec, mirrored = _Recorder(), []
    _run_audit(rec, mirrored)

    stamps = [rec_["created_at"] for _t, rec_ in mirrored]
    assert stamps, "no mirrored document — the test proved nothing"
    for s in stamps:
        assert s.tzinfo is None, f"expected naive UTC, got tz-aware {s!r}"
