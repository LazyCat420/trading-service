"""Row-shaped reads from MongoDB — the drop-in for a DB-API cursor fetch.

Why this module exists: application code reads query results POSITIONALLY.

    rows = db.execute("SELECT ticker, qty FROM positions WHERE bot=%s", [b]).fetchall()
    for r in rows:
        ticker, qty = r[0], r[1]

`mongo_store.find_docs()` returns dicts, so rewriting that call to it turns
every `r[0]` into a KeyError — or worse, silently reads a different field when
the code uses `.get()`. A mechanical rewrite is only safe if the replacement
returns the same SHAPE as the thing it replaces.

So these functions return tuples, in the column order the SQL asked for:

    rows = mongo_query.find_rows("positions", {"bot": b}, ["ticker", "qty"])

Every caller's positional indexing keeps working, unchanged, which is what
makes the codemod a mechanical transformation rather than a rewrite of 659
call sites by hand.

A missing field yields None, matching what Postgres returns for a NULL column —
not a KeyError, and not a silently dropped element that would shift every
later index in the tuple.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from app.db import mongo_store


def _project(columns: Sequence[str]) -> dict:
    proj = {c: 1 for c in columns}
    proj.setdefault("_id", 0)
    return proj


def _to_tuple(doc: dict, columns: Sequence[str]) -> tuple:
    # `doc.get(c)` and not `doc[c]`: a document written before a column was
    # added simply lacks the field, and Postgres would have returned NULL for
    # it. Raising here would fail a read that the SQL answered fine.
    return tuple(doc.get(c) for c in columns)


def find_rows(collection: str, query: dict[str, Any], columns: Sequence[str],
              sort: Optional[list] = None, limit: int = 0) -> list[tuple]:
    """`cursor.execute(SELECT ...).fetchall()` — rows as tuples in `columns` order."""
    docs = mongo_store.find_docs(collection, query, sort=sort,
                                 projection=_project(columns), limit=limit)
    return [_to_tuple(d, columns) for d in docs]


def find_row(collection: str, query: dict[str, Any], columns: Sequence[str],
             sort: Optional[list] = None) -> Optional[tuple]:
    """`cursor.execute(SELECT ...).fetchone()` — one row or None."""
    docs = mongo_store.find_docs(collection, query, sort=sort,
                                 projection=_project(columns), limit=1)
    return _to_tuple(docs[0], columns) if docs else None


def find_dicts(collection: str, query: dict[str, Any],
               sort: Optional[list] = None, limit: int = 0) -> list[dict]:
    """`SELECT *` — whole documents. Callers that unpack positionally cannot
    use this: a document has no column order. Those sites need their columns
    named explicitly, which is why the codemod refuses to rewrite `SELECT *`
    into a positional read."""
    return mongo_store.find_docs(collection, query, sort=sort, limit=limit)


def scalar(collection: str, query: dict[str, Any], column: str,
           sort: Optional[list] = None) -> Any:
    """One value from one row — `SELECT col FROM ... LIMIT 1` then `row[0]`."""
    row = find_row(collection, query, [column], sort=sort)
    return row[0] if row else None


def exists(collection: str, query: dict[str, Any]) -> bool:
    """`SELECT 1 FROM ... WHERE ... LIMIT 1` used as a boolean."""
    return mongo_store.count_docs(collection, query) > 0


def count(collection: str, query: Optional[dict] = None) -> int:
    """`SELECT count(*) FROM ... WHERE ...` — the one aggregate with an exact
    Mongo equivalent. Every other aggregate is refused by the translator and
    computed in Python at the call site."""
    return mongo_store.count_docs(collection, query or {})
