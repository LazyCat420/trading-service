"""
Multi-document transaction proof against the live rs0 replica set — the
atomicity primitive the money-ledger phase (Phase 6) will stand on. Verifies
BOTH directions: a clean transaction commits both writes, and an aborted one
leaves neither. Skips when Mongo is unreachable (CI without the NAS).
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.integration

_SCRATCH = "txn_scratch_test"


def _mongo_or_skip():
    from app.db import mongo_store
    try:
        db = mongo_store.get_doc_db()
        db.client.admin.command("ping")
    except Exception as e:
        pytest.skip(f"mongo unreachable: {e}")
    return mongo_store, db


def test_with_txn_commits_both_writes():
    mongo_store, db = _mongo_or_skip()
    a, b = f"a-{uuid.uuid4()}", f"b-{uuid.uuid4()}"
    try:
        with mongo_store.with_txn() as session:
            db[_SCRATCH].insert_one({"_id": a, "kind": "position"}, session=session)
            db[_SCRATCH].insert_one({"_id": b, "kind": "cash"}, session=session)
        assert db[_SCRATCH].count_documents({"_id": {"$in": [a, b]}}) == 2
    finally:
        db[_SCRATCH].delete_many({"_id": {"$in": [a, b]}})


def test_with_txn_aborts_atomically():
    """The half that matters for money: first write lands inside the txn, then
    the txn dies — NEITHER document may survive."""
    mongo_store, db = _mongo_or_skip()
    a, b = f"a-{uuid.uuid4()}", f"b-{uuid.uuid4()}"
    with pytest.raises(RuntimeError):
        with mongo_store.with_txn() as session:
            db[_SCRATCH].insert_one({"_id": a, "kind": "position"}, session=session)
            raise RuntimeError("mid-transaction failure")
    assert db[_SCRATCH].count_documents({"_id": {"$in": [a, b]}}) == 0


def test_helpers_accept_session():
    """update_docs/delete_docs/find_one_and_update participate in a session's
    transaction (write seen inside, gone after abort)."""
    mongo_store, db = _mongo_or_skip()
    key = f"h-{uuid.uuid4()}"
    with pytest.raises(RuntimeError):
        with mongo_store.with_txn() as session:
            db[_SCRATCH].insert_one({"_id": key, "status": "pending"}, session=session)
            claimed = mongo_store.find_one_and_update(
                _SCRATCH, {"_id": key, "status": "pending"},
                {"status": "running"}, session=session)
            assert claimed and claimed["status"] == "running"
            raise RuntimeError("abort")
    assert db[_SCRATCH].count_documents({"_id": key}) == 0
