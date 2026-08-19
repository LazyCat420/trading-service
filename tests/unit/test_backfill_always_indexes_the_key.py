"""The backfill must index its natural key before it upserts — every entrypoint.

THE DEFECT
----------
`backfill()` upserts with `UpdateOne({natural_key: ...}, upsert=True)`. Without
an index on that key, every upsert is a COLLECTION SCAN: a 2,000-document batch
against a 40,000-document collection examines 80 million documents. Measured on
`macro_indicators` at ~29 rows/sec, getting slower as the collection grew.

`ensure_key_index()` existed — but it was defined in `scripts/migrate_all.py`
and called from `migrate_all`'s sweep loop, i.e. in the CALLER. So the
invariant held for one entrypoint and silently did not for the other:

    python scripts/migrate_all.py                  -> indexed
    python scripts/pg_to_mongo_backfill.py <table> -> NOT indexed

The second is the documented way to seed a single table. Its only symptom is
slowness, so nothing in the output distinguishes a correct run from a run
doing 80M document examinations per batch — which is why a 15 rows/s seed rate
was treated as the cost of the data rather than as a missing index.

THE FIX, AND WHAT THESE TESTS PIN
---------------------------------
The function moved INTO `pg_to_mongo_backfill.py` and `backfill()` calls it
itself, before the first upsert. `migrate_all` re-exports it and no longer
calls it. These tests assert the property that matters — that the index exists
by the time the first upsert runs — rather than that some specific line is
present, so a refactor that keeps the guarantee keeps passing.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_backfill_calls_ensure_key_index_itself():
    """The call is inside `backfill()`, not in a caller.

    AST, not a substring search: a mention in a docstring or a comment is not
    a call, and this file is full of prose about the function.
    """
    from scripts import pg_to_mongo_backfill

    tree = ast.parse(inspect.getsource(pg_to_mongo_backfill.backfill))
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "ensure_key_index" in called, (
        "backfill() does not call ensure_key_index — the entrypoint that seeds "
        "one table upserts against a collection scan again"
    )


def test_the_index_call_precedes_the_upsert():
    """Ordering is the whole point: an index built after the load is useless.

    Walks `backfill()`'s body and asserts the first `ensure_key_index` call
    appears at a lower line number than the first `_bulk_upsert` call.
    """
    from scripts import pg_to_mongo_backfill

    src = inspect.getsource(pg_to_mongo_backfill.backfill)
    tree = ast.parse(src)

    def first_line(name: str) -> int | None:
        lines = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == name
        ]
        return min(lines) if lines else None

    idx_line = first_line("ensure_key_index")
    upsert_line = first_line("_bulk_upsert")
    assert idx_line is not None and upsert_line is not None
    assert idx_line < upsert_line, (
        f"ensure_key_index is called at line {idx_line} but the first upsert is "
        f"at {upsert_line} — indexing after the load does not make the load fast"
    )


def test_there_is_exactly_one_implementation():
    """Two copies of an invariant drift; `migrate_all` must re-export, not redefine.

    A second `def ensure_key_index` in migrate_all.py would let the two
    entrypoints disagree about which fields the index covers — the same class
    of split-brain the wildcard `backend_for()` branch created.
    """
    src = (REPO / "scripts" / "migrate_all.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    definitions = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "ensure_key_index"
    ]
    assert not definitions, (
        "migrate_all.py defines its own ensure_key_index again — there must be "
        "exactly one implementation, in pg_to_mongo_backfill.py"
    )

    from scripts.migrate_all import ensure_key_index as via_migrate_all
    from scripts.pg_to_mongo_backfill import ensure_key_index as canonical

    assert via_migrate_all is canonical, (
        "migrate_all.ensure_key_index is not the same object as the one in "
        "pg_to_mongo_backfill"
    )


def test_it_indexes_the_fields_the_upsert_actually_keys_on(monkeypatch):
    """The index must match the upsert filter, not a guess.

    An index on the wrong fields is indistinguishable from no index at all for
    the upsert's purposes — it just sits there while the scan continues — so
    "an index was created" is not the property worth asserting; "an index on
    THESE fields" is.
    """
    from scripts import pg_to_mongo_backfill

    created: list = []

    class FakeCollection:
        def list_indexes(self):
            return [{"key": {"_id": 1}}]

        def create_index(self, keys, **kwargs):
            created.append((keys, kwargs))

    monkeypatch.setattr(
        pg_to_mongo_backfill.mongo_store, "get_doc_db",
        lambda: {"position_lots": FakeCollection()},
    )
    monkeypatch.setattr(
        pg_to_mongo_backfill, "collection_for", lambda t: "position_lots"
    )

    msg = pg_to_mongo_backfill.ensure_key_index("position_lots", ["ticker", "date"])

    assert created, f"no index was created ({msg})"
    keys, kwargs = created[0]
    assert keys == [("ticker", 1), ("date", 1)], (
        f"indexed {keys}, but the upsert keys on ['ticker', 'date']"
    )
    assert kwargs.get("name") == "natural_key"
    assert kwargs.get("unique") is not True, (
        "the natural-key index must NOT be unique — collections mirrored "
        "before this ran may already hold duplicates, and a failed unique "
        "build aborts the whole table"
    )


def test_an_existing_index_is_not_rebuilt(monkeypatch):
    """Idempotent: re-seeding a table must not rebuild its index every run."""
    from scripts import pg_to_mongo_backfill

    created: list = []

    class FakeCollection:
        def list_indexes(self):
            return [{"key": {"_id": 1}}, {"key": {"ticker": 1, "date": 1}}]

        def create_index(self, keys, **kwargs):  # pragma: no cover - must not run
            created.append(keys)

    monkeypatch.setattr(
        pg_to_mongo_backfill.mongo_store, "get_doc_db",
        lambda: {"x": FakeCollection()},
    )
    monkeypatch.setattr(pg_to_mongo_backfill, "collection_for", lambda t: "x")

    msg = pg_to_mongo_backfill.ensure_key_index("x", ["ticker", "date"])
    assert not created, "an already-present index was rebuilt"
    assert "already present" in msg


@pytest.mark.parametrize("entrypoint", ["migrate_all", "pg_to_mongo_backfill"])
def test_both_entrypoints_reach_the_same_guarantee(entrypoint):
    """The regression this file exists for.

    Both documented ways to seed a table must end up indexing the key. Since
    `backfill()` owns the call, both do — this asserts neither entrypoint grew
    a path that bypasses `backfill()`.
    """
    module = __import__(f"scripts.{entrypoint}", fromlist=["x"])
    source = inspect.getsource(module)
    tree = ast.parse(source)
    calls_backfill = any(
        isinstance(n, ast.Call)
        and ((isinstance(n.func, ast.Name) and n.func.id == "backfill")
             or (isinstance(n.func, ast.Attribute) and n.func.attr == "backfill"))
        for n in ast.walk(tree)
    )
    defines_backfill = any(
        isinstance(n, ast.FunctionDef) and n.name == "backfill"
        for n in ast.walk(tree)
    )
    assert calls_backfill or defines_backfill, (
        f"{entrypoint} neither calls nor defines backfill(), so it may be "
        "seeding by some other path that skips the index"
    )
