"""Contracts of the RETAINED Postgres archive pool, kept where they belong.

`scripts/migration/pg_connection.py` survives the cutover as archive tooling —
`migrate_all`, `purge_bad_data`, `pg_quiescence` and the parity checkers still
open it. These two tests pin the two contract traps that shipped bugs, and they
lived until now inside `tests/unit/test_persistent_research_firm.py`, whose
three subjects — dossier_service, research_queue_service, question_ledger —
have all been Mongo-only since 2026-08-18. They were the last reason that file
imported the archive at all, which made it read as a Postgres-coupled test of
Mongo code.

Nothing about them changed; only where they live.

"""


def test_get_db_never_hands_back_something_you_can_execute_on():
    """The invariant both services violated, asserted against whatever
    `get_db` currently is — the real one, or the autouse test fake.

    Pinning it here rather than to `@contextmanager` internals is deliberate:
    the fixture must be held to the same contract as production, because the
    previous fixture satisfied both the correct and the incorrect usage and so
    could not fail. If the fake is ever loosened back to
    `MagicMock(return_value=cursor)`, this goes red.
    """
    from scripts.migration.pg_connection import get_db

    handle = get_db()
    assert not hasattr(handle, "execute"), (
        "get_db() returned something with .execute — the contract is "
        "`with get_db() as db:`, and a fake that allows `db = get_db()` "
        "cannot catch the bug that shipped on 2026-08-07"
    )
    with get_db() as db:
        assert hasattr(db, "execute"), "the yielded object must be a cursor"



def test_pooled_cursor_has_no_rowcount_so_counts_must_use_returning():
    """The second contract trap in the same file.

    `PooledCursor` wraps a psycopg cursor but exposes no `rowcount` and no
    `__getattr__` passthrough. Code that counts affected rows with
    `cur.rowcount` raises `AttributeError`, and inside a `try/except` that
    becomes a metric permanently reporting 0 — indistinguishable from "nothing
    happened". Both ledger updaters use `RETURNING id` instead.
    """
    from scripts.migration.pg_connection import PooledCursor

    assert not hasattr(PooledCursor, "rowcount")
    assert not hasattr(PooledCursor, "__getattr__"), (
        "a passthrough would make rowcount work again — if one is added "
        "deliberately, delete this test and say so"
    )


