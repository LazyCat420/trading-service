"""context_blobs must write ONE timestamp across every store it touches.

PG used to take `created_at` from the column default (CURRENT_TIMESTAMP, which
is transaction-start time) while the Mongo mirror took a Python
`datetime.now()` computed moments earlier. Two clocks for one fact. They agreed
whenever both landed in the same millisecond, so counts and sampled field
verification both scored it OK — an exhaustive sweep found 117 of 56,452 rows
drifted, Mongo always the earlier, by 1.0-6.3 ms.

Postgres is out of this path now: `log_rlm_audit_trail` writes `context_blobs`
and `llm_audit_logs` through `mongo_store` only. The defect these tests were
written to catch survives the cutover in a smaller form — the function still
stamps several documents in one call, and a `datetime.now()` recomputed per
document would reintroduce the same per-row drift within Mongo itself. So the
assertion is unchanged in substance: ONE object reaches every write, and it is
UTC.
"""
from datetime import datetime, timezone
from unittest.mock import patch

from app.services import rlm_audit


def _run_audit():
    """Drive log_rlm_audit_trail, capturing every Mongo write it issues.

    Returns (upserts, inserts): upsert_doc calls as (collection, key, doc) and
    insert_docs calls as (collection, docs).
    """
    upserts: list[tuple] = []
    inserts: list[tuple] = []

    def _upsert_doc(collection, key, doc, **kw):
        upserts.append((collection, key, doc))

    def _insert_docs(collection, docs, *a, **kw):
        inserts.append((collection, docs))

    with patch.object(rlm_audit.mongo_store, "upsert_doc", _upsert_doc), \
         patch.object(rlm_audit.mongo_store, "insert_docs", _insert_docs):
        rlm_audit.log_rlm_audit_trail(
            cycle_id="cycle-test", bot_id="bot-test", ticker="AAPL",
            context="ctx", trading_system_prompt="sys", active_model="m",
            response_text="resp", tokens_used=1, execution_time=0.5,
        )
    return upserts, inserts


def _blob_upserts(upserts):
    return [(key, doc) for coll, key, doc in upserts if coll == "context_blobs"]


def test_pg_insert_supplies_created_at_explicitly():
    """Every stored blob must carry an explicit `created_at`.

    Left to a store-side default the two writes cannot be made to agree; the
    value has to be supplied by the caller, which is what this checks.
    """
    upserts, _inserts = _run_audit()

    blobs = _blob_upserts(upserts)
    assert blobs, "no context_blobs write was issued — the test proved nothing"
    # The context and the system prompt are two distinct blobs, deduped by
    # hash; both must be stamped.
    assert len(blobs) == 2, f"expected the context and prompt blobs, got {len(blobs)}"
    for key, doc in blobs:
        assert "created_at" in doc, (
            "context_blobs write does not supply created_at, so the store is "
            "left to its own clock"
        )
        assert isinstance(doc["created_at"], datetime), \
            "no datetime was written to created_at"
        # Dedup is by content hash: a write keyed on anything else would store
        # one row per call and the blob table would stop deduping.
        assert set(key) == {"context_hash"}
        assert key["context_hash"] == doc["context_hash"]


def test_both_stores_receive_the_identical_timestamp():
    """The real assertion: same value, not merely both present."""
    upserts, inserts = _run_audit()

    blobs = _blob_upserts(upserts)
    assert blobs, "no context_blobs write was issued — the test proved nothing"
    audit_rows = [d for coll, docs in inserts if coll == "llm_audit_logs" for d in docs]
    assert audit_rows, "no llm_audit_logs row was written — the test proved nothing"

    blob_times = [doc["created_at"] for _key, doc in blobs]
    audit_times = [row["created_at"] for row in audit_rows]

    # Every document in one call shares one timestamp — the blobs with each
    # other, and with the audit row that references them by hash.
    assert len(set(blob_times)) == 1, \
        f"the blobs got {len(set(blob_times))} distinct timestamps"
    assert set(blob_times) == set(audit_times), (
        f"blobs wrote {sorted(set(blob_times))} but the audit row wrote "
        f"{sorted(set(audit_times))} — two clocks for one fact"
    )

    # Same OBJECT, not merely an equal one: identity is what makes the
    # agreement hold by construction rather than by landing in the same
    # millisecond, which is exactly how the original drift hid.
    assert all(t is blob_times[0] for t in blob_times + audit_times)


def test_timestamp_is_naive_utc_matching_the_column_type():
    """The stamp must be UTC, unambiguously.

    Under Postgres this meant naive-UTC, because `created_at` was `timestamp
    without time zone` and a tz-aware value would be cast by the session
    TimeZone — a whole-hour shift, silently. Mongo stores UTC datetimes, so
    the equivalent requirement is that the value is explicitly UTC and never a
    naive local-clock reading, which would be stored as if it were UTC and
    shift by the host's offset.
    """
    upserts, _inserts = _run_audit()

    stamps = [doc["created_at"] for _key, doc in _blob_upserts(upserts)]
    assert stamps, "no stored blob — the test proved nothing"
    for s in stamps:
        assert s.tzinfo is not None, (
            f"expected an explicitly-UTC stamp, got naive {s!r}: a naive local "
            "clock is stored as though it were UTC"
        )
        assert s.utcoffset() == timezone.utc.utcoffset(None), \
            f"expected UTC, got offset {s.utcoffset()}"
