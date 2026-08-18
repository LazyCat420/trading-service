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


def _clean_val(v: Any, *, as_decimal: bool = False) -> Any:
    """Unwrap a Decimal128 on the way out of Mongo.

    WHY THIS HAS TWO ANSWERS
    ------------------------
    Postgres stored every money column as DOUBLE PRECISION — 328 of them, and
    not one NUMERIC in the whole schema — so psycopg handed back floats and
    every caller was written against floats. Decimal128 is therefore an
    UPGRADE the migration is making deliberately ("switch to Decimal128 during
    migration, don't preserve", 2026-07-21), not a shape to preserve.

    An upgrade that stops at the storage layer is not an upgrade: writing
    Decimal128 and reading float back means every P&L sum, every cash
    adjustment and every average entry price is still float arithmetic, and
    the ledger merely *looks* exact at rest. So money collections now read as
    `decimal.Decimal`.

    Everything else keeps reading as float, because those callers pass values
    into numpy, json and pandas, where a Decimal is a TypeError rather than a
    precision improvement.

    NOT YET, AND HERE IS THE MEASUREMENT
    ------------------------------------
    Flipping money reads to Decimal was tried on 2026-08-18 and reverted the
    same hour. It works — `find_row("bots", ...)` returns exact Decimals and
    100000.07 + 0.03 gives 100000.10 rather than 100000.09999999999 — but the
    money path mixes money with things that are NOT money: ratios
    (`take_profit_pct`), vendor quotes (`price_history.close`, still float),
    and share counts. `entry_price * (1 + effective_tp)` raises TypeError the
    moment entry_price is a Decimal, and that is one of 39 such sites across
    8 modules (paper_trader 13, bot_manager 11, portfolio 10).

    Doing it properly means deciding, per boundary, whether the float side is
    promoted or the Decimal side demoted — and proving the result with the
    cent-exact reconciliation artifact Tier F requires, not with a green
    suite. That is phase S3, and it is a change to the money path that
    deserves its own pass rather than a tail-end edit.

    The `as_decimal` parameter and `_money()` below are the plumbing, already
    wired and tested; only the flip is deferred.
    """
    if v is None:
        return None
    if hasattr(v, "to_decimal"):
        # STILL FLOAT, deliberately — see the NOT-YET note below.
        return float(str(v))
    return v


def _to_tuple(doc: dict, columns: Sequence[str], *, as_decimal: bool = False) -> tuple:
    # `doc.get(c)` and not `doc[c]`: a document written before a column was
    # added simply lacks the field, and Postgres would have returned NULL for
    # it. Raising here would fail a read that the SQL answered fine.
    return tuple(_clean_val(doc.get(c), as_decimal=as_decimal) for c in columns)


def _money(collection: str) -> bool:
    """Does this collection carry the dec128 numeric policy?

    Read from the same table_spec helper the WRITE path uses, so the two sides
    cannot disagree about which collections are money.
    """
    try:
        from app.db.table_spec import uses_decimal128

        return uses_decimal128(collection)
    except Exception:  # noqa: BLE001 - a missing ledger must not break reads
        return False


def find_rows(collection: str, query: dict[str, Any], columns: Sequence[str],
              sort: Optional[list] = None, limit: int = 0,
              session: Optional[Any] = None) -> list[tuple]:
    """`cursor.execute(SELECT ...).fetchall()` — rows as tuples in `columns` order."""
    docs = mongo_store.find_docs(collection, query, sort=sort,
                                 projection=_project(columns), limit=limit,
                                 session=session)
    as_dec = _money(collection)
    return [_to_tuple(d, columns, as_decimal=as_dec) for d in docs]


def find_row(collection: str, query: dict[str, Any], columns: Sequence[str],
             sort: Optional[list] = None,
             session: Optional[Any] = None) -> Optional[tuple]:
    """`cursor.execute(SELECT ...).fetchone()` — one row or None."""
    docs = mongo_store.find_docs(collection, query, sort=sort,
                                 projection=_project(columns), limit=1,
                                 session=session)
    return _to_tuple(docs[0], columns, as_decimal=_money(collection)) if docs else None


def find_dicts(collection: str, query: dict[str, Any],
               sort: Optional[list] = None, limit: int = 0,
               session: Optional[Any] = None) -> list[dict]:
    """`SELECT *` — whole documents."""
    return mongo_store.find_docs(collection, query, sort=sort, limit=limit,
                                session=session)


def scalar(collection: str, query: dict[str, Any], column: str,
           sort: Optional[list] = None,
           session: Optional[Any] = None) -> Any:
    """One value from one row — `SELECT col FROM ... LIMIT 1` then `row[0]`."""
    row = find_row(collection, query, [column], sort=sort, session=session)
    return row[0] if row else None


def agg_row(collection: str, query: dict[str, Any],
            aggs: Sequence[tuple[str, Any]],
            session: Optional[Any] = None) -> tuple:
    """`SELECT COUNT(*), MIN(x), MAX(x) FROM t WHERE ...` as one tuple.

    Each entry in `aggs` is (op, field), matching the SELECT list order:

        ("count", None)          COUNT(*)
        ("count", "col")         COUNT(col)      — skips NULLs, as SQL does
        ("count_distinct", "c")  COUNT(DISTINCT c)
        ("min"|"max"|"avg"|"sum", "col")
        ("count_null", "col")    SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END)

    Done as a `$group` pipeline, NOT by fetching documents and reducing in
    Python. The tables these run against include price_history at 15.7M rows;
    pulling those over the wire to count them would replace a fast query with a
    slow one, and the counting is the only part Python would contribute.

    Returns the same shape `cursor.fetchone()` returned: a tuple, in SELECT
    order. An empty match yields SQL's answer for an empty group — 0 for the
    counts, None for min/max/avg/sum — rather than an empty tuple, which would
    make callers index out of range where the SQL gave them a row.
    """
    group: dict[str, Any] = {"_id": None}
    for i, (op, field) in enumerate(aggs):
        key = f"a{i}"
        if op == "count" and field is None:
            group[key] = {"$sum": 1}
        elif op == "count":
            # SQL COUNT(col) does not count NULLs.
            group[key] = {"$sum": {"$cond": [{"$eq": [f"${field}", None]}, 0, 1]}}
        elif op == "count_null":
            group[key] = {"$sum": {"$cond": [{"$eq": [f"${field}", None]}, 1, 0]}}
        elif op == "count_distinct":
            group[key] = {"$addToSet": f"${field}"}
        elif op in ("min", "max", "avg", "sum"):
            group[key] = {f"${op}": f"${field}"}
        else:
            raise ValueError(f"unsupported aggregate {op!r}")

    pipeline = ([{"$match": query}] if query else []) + [{"$group": group}]
    rows = mongo_store.aggregate(collection, pipeline, session=session)
    if not rows:
        return tuple(0 if op.startswith("count") else None for op, _ in aggs)
    doc = rows[0]
    out = []
    for i, (op, _) in enumerate(aggs):
        v = doc.get(f"a{i}")
        if op == "count_distinct":
            # $addToSet drops duplicates but keeps NULL; SQL COUNT(DISTINCT c)
            # does not count it.
            v = len([x for x in (v or []) if x is not None])
        out.append(v)
    return tuple(out)


def group_rows(collection: str, query: dict[str, Any],
               keys: Sequence[str], aggs: Sequence[tuple[str, Any]],
               select: Sequence[tuple[str, Any]],
               sort: Optional[list] = None, limit: int = 0) -> list[tuple]:
    """`SELECT k, COUNT(*) FROM t WHERE ... GROUP BY k` as a list of tuples.

    `keys`   the GROUP BY columns.
    `aggs`   the aggregates, same (op, field) vocabulary as agg_row().
    `select` the SELECT list in ORDER, as ("key", col) or ("agg", index) —
             SQL lets you write the aggregate before the key, so the output
             order cannot be inferred from keys+aggs and is passed explicitly.

    Returns tuples in SELECT order, matching `cursor.fetchall()`, so callers
    that unpack positionally keep working.

    `sort` is applied INSIDE the pipeline over the output names, because
    ORDER BY on a grouped query sorts the groups, not the documents — sorting
    the input and grouping afterwards would silently reorder the result.
    """
    group: dict[str, Any] = {"_id": {k: f"${k}" for k in keys} if keys else None}
    for i, (op, field) in enumerate(aggs):
        name = f"a{i}"
        if op == "count" and field is None:
            group[name] = {"$sum": 1}
        elif op == "count":
            group[name] = {"$sum": {"$cond": [{"$eq": [f"${field}", None]}, 0, 1]}}
        elif op == "count_null":
            group[name] = {"$sum": {"$cond": [{"$eq": [f"${field}", None]}, 1, 0]}}
        elif op == "count_distinct":
            group[name] = {"$addToSet": f"${field}"}
        elif op in ("min", "max", "avg", "sum"):
            group[name] = {f"${op}": f"${field}"}
        else:
            raise ValueError(f"unsupported aggregate {op!r}")

    pipeline: list[dict] = ([{"$match": query}] if query else []) + [{"$group": group}]
    if sort:
        pipeline.append({"$sort": {
            (f"_id.{c}" if any(c == k for k in keys) else c): d for c, d in sort
        }})
    if limit:
        pipeline.append({"$limit": limit})

    out = []
    for doc in mongo_store.aggregate(collection, pipeline):
        row = []
        for kind, ref in select:
            if kind == "key":
                row.append((doc.get("_id") or {}).get(ref))
            else:
                v = doc.get(f"a{ref}")
                if aggs[ref][0] == "count_distinct":
                    v = len([x for x in (v or []) if x is not None])
                row.append(v)
        out.append(tuple(row))
    return out


def join_rows(left: str, left_query: dict[str, Any], left_key: str,
              right: str, right_key: str, right_query: Optional[dict] = None,
              left_fields: Sequence[str] = (), right_fields: Sequence[str] = (),
              select: Sequence[tuple[str, str]] = (),
              sort: Optional[list] = None, limit: int = 0) -> list[tuple]:
    """An INNER JOIN on one equality, done as two queries plus a Python stitch.

    Deliberately NOT $lookup. $lookup on an unindexed foreign field does a
    collection scan per input document, and its left-outer semantics differ
    from an INNER JOIN — a non-matching row comes back with an empty array
    rather than being dropped, so a careless port turns a filter into a
    pass-through.

    Rows are emitted once per matching right document, which is what an INNER
    JOIN does when the right side is not unique on the key. `select` is
    ("l"|"r", column) in SELECT order.
    """
    r_docs = mongo_store.find_docs(
        right, right_query or {},
        projection={f: 1 for f in list(right_fields) + [right_key]} | {"_id": 0})
    index: dict[Any, list[dict]] = {}
    for d in r_docs:
        index.setdefault(d.get(right_key), []).append(d)

    l_docs = mongo_store.find_docs(
        left, left_query,
        projection={f: 1 for f in list(left_fields) + [left_key]} | {"_id": 0},
        sort=sort)

    out = []
    for l in l_docs:
        for r in index.get(l.get(left_key), ()):      # inner join: no match, no row
            out.append(tuple((l if side == "l" else r).get(col)
                             for side, col in select))
            if limit and len(out) >= limit:
                return out
    return out


def exists(collection: str, query: dict[str, Any]) -> bool:
    """`SELECT 1 FROM ... WHERE ... LIMIT 1` used as a boolean."""
    return mongo_store.count_docs(collection, query) > 0


def count(collection: str, query: Optional[dict] = None) -> int:
    """`SELECT count(*) FROM ... WHERE ...` — the one aggregate with an exact
    Mongo equivalent. Every other aggregate is refused by the translator and
    computed in Python at the call site."""
    return mongo_store.count_docs(collection, query or {})
