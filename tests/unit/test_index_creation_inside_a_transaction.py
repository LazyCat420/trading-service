"""Index creation must not run inside an open Mongo transaction.

THE DEFECT
----------
`insert_docs` and `upsert_doc` call `ensure_indexes()` before writing.
`ensure_indexes()` issues `create_index` for ~30 collections, and
`create_index` on a collection that does not exist yet CREATES it. That is a
catalog write, and it aborts any transaction in flight:

    Collection namespace 'trading_bot.lot_closures' is already in use.
    ... Transaction with { txnNumber: N } has been aborted.  (code 251)

`paper_trader.sell()` is exactly that shape — it opens `with_txn()` and then
inserts into `lot_closures`, `orders` and `trade_fills`. So on a store whose
collections do not exist yet, the FIRST transactional SELL of a process aborts.
The money path, failing on a fresh deployment, in the one code path wrapped in
a transaction precisely because it must not half-apply.

TWO CONDITIONS, AND THE BUG NEEDS BOTH
--------------------------------------
  1. `_indexes_ready` is False (the DDL has not run yet), AND
  2. the target collections do not exist (so the DDL writes the catalog).

Miss either and the transaction survives. The first version of this file set
only condition 1 and reported four green tests over a live bug — which is why
`test_the_fixture_really_reproduces_the_preconditions` asserts the
preconditions instead of assuming them, and why every test below writes to a
UNIQUE collection name: within one file, an earlier test creates the shared
collections and silently disarms every test after it.

WHY IT SURVIVED SO LONG
-----------------------
`_indexes_ready` is a process global, so in a long-lived container the first
write is usually not in a transaction and the flag is already set by the time a
SELL runs. And both pure-Mongo E2E tests stubbed the function out entirely
(`monkeypatch.setattr(mongo_store, "ensure_indexes", lambda: None)`) — a
harness flag disabling the only failure mode this path has.
"""
from __future__ import annotations

import uuid

import pytest

from app.db import mongo_store

pytestmark = pytest.mark.real_mongo


@pytest.fixture
def fresh_namespace(real_mongo, monkeypatch):
    """A collection name that has never existed, with the DDL still pending.

    Both preconditions, per test. `collection_for` is patched so a real table
    name routes to a unique physical collection — the write path is unchanged,
    but the namespace is guaranteed absent, which is what makes the catalog
    write (and therefore the abort) reachable.
    """
    name = f"txn_ddl_{uuid.uuid4().hex[:10]}"

    from app.db import collections as db_collections

    real_collection_for = db_collections.collection_for
    monkeypatch.setattr(
        db_collections,
        "collection_for",
        lambda t: name if t in ("lot_closures", "positions") else real_collection_for(t),
    )

    original = mongo_store._indexes_ready
    mongo_store._indexes_ready = False
    yield name, real_mongo
    mongo_store._indexes_ready = original
    real_mongo[name].delete_many({})


def test_the_fixture_really_reproduces_the_preconditions(fresh_namespace):
    """NEGATIVE CONTROL for the fixture itself.

    If the namespace already existed, `create_index` would be a no-op on the
    catalog and the tests below would pass for the wrong reason.
    """
    name, db = fresh_namespace
    assert mongo_store._indexes_ready is False, "the DDL must still be pending"
    assert name not in set(db.list_collection_names()), (
        "the target collection already exists, so createIndexes will not write "
        "the catalog and these tests cannot see the abort"
    )


def test_a_write_inside_a_transaction_survives_pending_index_creation(fresh_namespace):
    """The regression test for the abort.

    Without the guard in `ensure_indexes`, this raises OperationFailure
    ("Collection namespace ... is already in use", code 251).
    """
    name, db = fresh_namespace

    with mongo_store.with_txn() as session:
        mongo_store.insert_docs(
            "lot_closures",
            [{"closure_id": "txn-ddl-1", "bot_id": "b", "ticker": "T",
              "realized_pnl": mongo_store.dec128("1.00")}],
            session=session,
        )

    assert db[name].count_documents({"closure_id": "txn-ddl-1"}) == 1, (
        "the insert did not commit — the transaction was aborted by index "
        "creation writing the catalog underneath it"
    )


def test_upsert_inside_a_transaction_survives_pending_index_creation(fresh_namespace):
    """`upsert_doc` reaches `ensure_indexes` on the same path."""
    name, db = fresh_namespace

    with mongo_store.with_txn() as session:
        mongo_store.upsert_doc(
            "positions",
            {"id": "txn-ddl-2"},
            {"id": "txn-ddl-2", "bot_id": "b", "ticker": "T", "qty": 1.0},
            session=session,
        )

    assert db[name].count_documents({"id": "txn-ddl-2"}) == 1


def test_the_indexes_are_deferred_not_deleted(fresh_namespace):
    """The fix must DEFER the DDL, not skip it forever.

    Skipping index creation inside a transaction is only correct if the
    indexes are still built once nothing is in a transaction — otherwise the
    fix trades an abort for an unindexed collection, and the collection-scan
    upserts that cost the backfill ~15 rows/s come back.
    """
    _, db = fresh_namespace

    # Inside a transaction: deferred, so the flag must NOT be set.
    with mongo_store.with_txn() as session:
        mongo_store.insert_docs(
            "lot_closures", [{"closure_id": "txn-ddl-3"}], session=session
        )
    assert mongo_store._indexes_ready is False, (
        "the in-transaction call set the readiness flag, so the real index "
        "build is now skipped forever — deferred became never"
    )

    # Outside one: the DDL runs for real.
    mongo_store.ensure_indexes()
    assert mongo_store._indexes_ready is True
    names = {i["name"] for i in db["decision_outcomes"].list_indexes()}
    assert len(names) > 1, (
        "decision_outcomes carries only the default _id index — "
        "ensure_indexes() stopped creating indexes entirely"
    )


def test_a_session_not_in_a_transaction_still_builds_indexes(real_mongo):
    """Passing a session is not the same as being IN a transaction.

    The guard keys on `session.in_transaction`, not on `session is not None`.
    A caller that passes a plain session (several read helpers do) must still
    get its indexes built, or the deferral would swallow the DDL for the whole
    process.
    """
    original = mongo_store._indexes_ready
    mongo_store._indexes_ready = False
    try:
        client = mongo_store.get_mongo_client()
        with client.start_session() as session:
            assert not session.in_transaction
            mongo_store.ensure_indexes(session)
        assert mongo_store._indexes_ready is True, (
            "a session that is NOT in a transaction was treated as one, so "
            "the indexes were skipped"
        )
    finally:
        mongo_store._indexes_ready = original


def test_boot_leaves_the_flag_unset_which_is_why_this_can_happen():
    """Pins the second half of the diagnosis so it cannot be argued away.

    `init_mongo_schema()` is the boot stage that sounds like it prepares the
    trading collections. It does not call `mongo_store.ensure_indexes()` — it
    indexes the Civilization Council collections in the `prism` database. So
    after boot the DDL is still pending and the first transactional write is
    the one that trips.

    If someone later makes boot call ensure_indexes(), this test fails and
    should be updated deliberately — but the in-transaction guard must stay
    regardless, because a process can always reach a transaction first.
    """
    import inspect

    from app.db import mongo as mongo_module

    source = inspect.getsource(mongo_module.init_mongo_schema)
    assert "ensure_indexes" not in source, (
        "init_mongo_schema now calls ensure_indexes — update this test's "
        "premise, but KEEP the in-transaction guard"
    )
