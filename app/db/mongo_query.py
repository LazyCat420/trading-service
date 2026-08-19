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

from decimal import Decimal
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

    WHY THE FIRST ATTEMPT WAS REVERTED, AND WHAT CHANGED
    ----------------------------------------------------
    Flipping this on 2026-08-18 raised `TypeError: unsupported operand type(s)
    for *: 'Decimal' and 'float'`, and the flip was reverted. The diagnosis
    recorded at the time — that the money path "mixes money with things that
    are NOT money" — was right. The granularity was the bug: the decision was
    read per TABLE, so `positions.stop_loss_pct` (a ratio) and `positions.qty`
    (a share count) were promoted to Decimal along with `avg_entry_price`, and
    `entry_price * (1 + effective_tp)` broke on the ratio, not on the money.

    The policy is now per COLUMN (`table_spec.column_is_money`), applied to
    BOTH halves — `_coerce()` writes and `_clean_val()` reads resolve through
    the same function. Of the 25 numeric columns across the 7 money
    collections, 9 are share counts and 3 are ratios; the other 13 are settled
    amounts and are the only ones that become Decimal.

    That reduces the boundary problem rather than solving it by declaration:
    money still meets float vendor quotes (`price_history.close`), and each
    such site is fixed by promoting the float, never by demoting the Decimal —
    demoting would discard the exactness the column was promoted for.
    """
    if v is None:
        return None
    if hasattr(v, "to_decimal"):
        # Decimal128 -> str -> Decimal, never through float: routing an exact
        # value through a float would throw away the exactness the column was
        # promoted for.
        return Decimal(str(v)) if as_decimal else float(str(v))
    return v


def _to_tuple(doc: dict, columns: Sequence[str], *, money_cols: frozenset[str] = frozenset()) -> tuple:
    # `doc.get(c)` and not `doc[c]`: a document written before a column was
    # added simply lacks the field, and Postgres would have returned NULL for
    # it. Raising here would fail a read that the SQL answered fine.
    return tuple(
        _clean_val(doc.get(c), as_decimal=c in money_cols) for c in columns
    )


def _money_cols(collection: str, columns: Sequence[str]) -> frozenset[str]:
    """Which of `columns` are money in `collection`.

    Resolved per COLUMN, through the same `table_spec` helper the WRITE path
    uses, so the two halves of the money contract cannot disagree about a
    field: anything stored as Decimal128 reads back as Decimal, and anything
    stored as float reads back as float.

    Per column and not per table because a money collection also carries
    things that are not money — `positions.stop_loss_pct` is a ratio,
    `positions.qty` is a share count — and promoting those to Decimal raises
    TypeError the moment they meet a float quote. See
    `table_spec.column_is_money`.
    """
    try:
        from app.db.table_spec import column_is_money

        return frozenset(c for c in columns if column_is_money(collection, c))
    except Exception:  # noqa: BLE001 - a missing ledger must not break reads
        return frozenset()


def as_money(value: Any) -> Any:
    """Promote a float/int to `Decimal` so it can meet money in arithmetic.

    THE RULE AT A MONEY/FLOAT BOUNDARY: promote the float, never demote the
    Decimal. Money read from Mongo is exact; a vendor quote, a ratio or a share
    count arriving as float is not. `Decimal(x) * float(y)` raises TypeError,
    and the two ways to silence it are not equivalent —

        float(entry_price) * tp        discards the exactness the column was
                                       promoted for, at every call site, and
                                       silently reintroduces float drift into
                                       a value the ledger reconciles on
        entry_price * as_money(tp)     keeps the result exact

    — so this exists to make the correct direction the short one.

    Via `str()`, not `Decimal(float)`: `Decimal(0.08)` is
    0.08000000000000000166533453693773481063544750213623046875, whereas
    `Decimal("0.08")` is exactly 0.08. The float already carries whatever error
    it carries; printing it is the closest recoverable value, and it is what
    `mongo_store._money()` does on the write side, so a value that round-trips
    through both is unchanged.

    `None` passes through as `None`, matching every other helper here: a NULL
    column stays NULL rather than becoming `Decimal("0")`, which would turn a
    missing stop-loss into a real one.
    """
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # bool subclasses int; a flag is not an amount.
        raise TypeError("as_money() received a bool, which is not an amount")
    return Decimal(str(value))


def find_rows(collection: str, query: dict[str, Any], columns: Sequence[str],
              sort: Optional[list] = None, limit: int = 0,
              session: Optional[Any] = None) -> list[tuple]:
    """`cursor.execute(SELECT ...).fetchall()` — rows as tuples in `columns` order."""
    docs = mongo_store.find_docs(collection, query, sort=sort,
                                 projection=_project(columns), limit=limit,
                                 session=session)
    money = _money_cols(collection, columns)
    return [_to_tuple(d, columns, money_cols=money) for d in docs]


def find_row(collection: str, query: dict[str, Any], columns: Sequence[str],
             sort: Optional[list] = None,
             session: Optional[Any] = None) -> Optional[tuple]:
    """`cursor.execute(SELECT ...).fetchone()` — one row or None."""
    docs = mongo_store.find_docs(collection, query, sort=sort,
                                 projection=_project(columns), limit=1,
                                 session=session)
    if not docs:
        return None
    return _to_tuple(docs[0], columns, money_cols=_money_cols(collection, columns))


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
    money = _money_cols(collection, [f for _, f in aggs if f])
    out = []
    for i, (op, field) in enumerate(aggs):
        v = doc.get(f"a{i}")
        if op == "count_distinct":
            # $addToSet drops duplicates but keeps NULL; SQL COUNT(DISTINCT c)
            # does not count it.
            v = len([x for x in (v or []) if x is not None])
        else:
            # $sum/$avg/$min/$max over a Decimal128 column returns a raw
            # bson.Decimal128, which is neither a number a caller can do
            # arithmetic on nor something `format()` understands —
            # `f"{Decimal128('30.03'):.2f}"` raises TypeError. Postgres handed
            # back a plain number here, so unwrap it the same way the row
            # readers do, honouring the same per-column money policy: an
            # aggregate over money is money.
            #
            # `$avg` is deliberately included: Mongo computes the average of
            # Decimal128 inputs in decimal, so demoting it to float here would
            # discard the exactness before the caller ever sees it.
            v = _clean_val(v, as_decimal=field in money)
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

    # Same unwrapping as agg_row: a grouped $sum over a Decimal128 column
    # returns a raw bson.Decimal128, and a GROUP BY key can itself be a money
    # column. Both must come back as numbers, under the same per-column policy.
    referenced = [ref for kind, ref in select if kind == "key"]
    referenced += [aggs[ref][1] for kind, ref in select
                   if kind != "key" and aggs[ref][1]]
    money = _money_cols(collection, referenced)

    out = []
    for doc in mongo_store.aggregate(collection, pipeline):
        row = []
        for kind, ref in select:
            if kind == "key":
                v = (doc.get("_id") or {}).get(ref)
                row.append(_clean_val(v, as_decimal=ref in money))
            else:
                v = doc.get(f"a{ref}")
                if aggs[ref][0] == "count_distinct":
                    v = len([x for x in (v or []) if x is not None])
                else:
                    v = _clean_val(v, as_decimal=aggs[ref][1] in money)
                row.append(v)
        out.append(tuple(row))
    return out


def _index_by(docs, key: str) -> dict:
    """Right-hand docs grouped by join key, MINUS the ones with no key.

    `NULL = NULL` is not true in SQL, so a missing join key matches nothing.
    Grouping them under `None` instead would join every keyless right document
    to every keyless left one — a cross product of exactly the rows that should
    not have joined at all, and one that no row count would look wrong.
    """
    index: dict[Any, list[dict]] = {}
    for d in docs:
        k = d.get(key)
        if k is not None:
            index.setdefault(k, []).append(d)
    return index


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
    index = _index_by(r_docs, right_key)

    l_docs = mongo_store.find_docs(
        left, left_query,
        projection={f: 1 for f in list(left_fields) + [left_key]} | {"_id": 0},
        sort=sort)

    # Money resolves against the collection the column comes FROM: a join can
    # select `positions.avg_entry_price` (money) beside `technicals.rsi_14`
    # (not), and the side decides which policy applies. Without this a joined
    # money column comes back as a raw bson.Decimal128 while the same column
    # read through find_rows() comes back as Decimal — the same field with two
    # types depending on which helper fetched it.
    l_money = _money_cols(left, [c for side, c in select if side == "l"])
    r_money = _money_cols(right, [c for side, c in select if side != "l"])

    out = []
    for l in l_docs:
        key = l.get(left_key)
        for r in (() if key is None else index.get(key, ())):  # no match, no row
            out.append(_stitch(l, r, select, l_money, r_money))
            if limit and len(out) >= limit:
                return out
    return out


def _stitch(l: dict, r: Optional[dict], select, l_money, r_money) -> tuple:
    """One output row. `r is None` is the unmatched left row of an outer join,
    where every right column is NULL — which is what SQL returns and what the
    callers' `is None` branches already test for."""
    return tuple(
        _clean_val((l if side == "l" else (r or {})).get(col),
                   as_decimal=col in (l_money if side == "l" else r_money))
        for side, col in select
    )


def left_join_rows(left: str, left_query: dict[str, Any], left_key: str,
                   right: str, right_key: str, right_query: Optional[dict] = None,
                   left_fields: Sequence[str] = (), right_fields: Sequence[str] = (),
                   select: Sequence[tuple[str, str]] = (),
                   sort: Optional[list] = None, limit: int = 0) -> list[tuple]:
    """A LEFT OUTER JOIN on one equality — every left row survives.

    Separate from `join_rows` rather than a flag on it, because the two return
    DIFFERENT ROW COUNTS from the same arguments and a flag is a thing a codemod
    can get wrong silently. An inner join used where the SQL said LEFT JOIN
    drops exactly the rows the outer join exists to keep: `sector_aggregator`
    hit this first — an inner stitch there turns "every S&P 500 name in this
    sector" into "the ones that happen to have a price row", i.e. a shorter
    listing that still looks complete.

    Unmatched left rows come back with every right column None, which is what
    Postgres returns and what the callers already branch on.
    """
    r_docs = mongo_store.find_docs(
        right, right_query or {},
        projection={f: 1 for f in list(right_fields) + [right_key]} | {"_id": 0})
    index = _index_by(r_docs, right_key)

    l_docs = mongo_store.find_docs(
        left, left_query,
        projection={f: 1 for f in list(left_fields) + [left_key]} | {"_id": 0},
        sort=sort)

    l_money = _money_cols(left, [c for side, c in select if side == "l"])
    r_money = _money_cols(right, [c for side, c in select if side != "l"])

    out = []
    for l in l_docs:
        key = l.get(left_key)
        matches = () if key is None else index.get(key, ())
        for r in matches or (None,):        # no match -> ONE row, right side NULL
            out.append(_stitch(l, r, select, l_money, r_money))
            if limit and len(out) >= limit:
                return out
    return out


def anti_join_rows(left: str, left_query: dict[str, Any], left_key: str,
                   right: str, right_key: str, right_query: Optional[dict] = None,
                   left_fields: Sequence[str] = (),
                   select: Sequence[tuple[str, str]] = (),
                   sort: Optional[list] = None, limit: int = 0) -> list[tuple]:
    """`LEFT JOIN r ON ... WHERE r.key IS NULL` — the left rows with NO match.

    THE TRAP THIS EXISTS FOR: an anti-join is the COMPLEMENT of an inner join,
    so translating one with `join_rows` does not return slightly wrong rows —
    it returns exactly the rows that should have been excluded, and every one
    of them looks plausible. "Tickers with no analysis yet" comes back as
    "tickers that already have one", the caller queues them, and the symptom is
    duplicated work rather than an error. Known consumers: `pending_review`,
    `smart_money_tools`, and two sites in `strategy_auditor`.

    `select` takes LEFT columns only: every right column of an anti-join is
    NULL by construction, so naming one is a sign the statement was not
    actually an anti-join.
    """
    bad = [c for side, c in select if side != "l"]
    if bad:
        raise ValueError(
            f"anti_join_rows selects only left columns; {bad} come from the "
            "right side, which is NULL for every row an anti-join returns — "
            "if the query needs them it is a LEFT JOIN (use left_join_rows), "
            "not an anti-join")

    # Only the key is needed from the right side: the question is membership.
    r_keys = {
        d.get(right_key)
        for d in mongo_store.find_docs(right, right_query or {},
                                       projection={right_key: 1, "_id": 0})
    } - {None}

    l_docs = mongo_store.find_docs(
        left, left_query,
        projection={f: 1 for f in list(left_fields) + [left_key]} | {"_id": 0},
        sort=sort)

    l_money = _money_cols(left, [c for side, c in select if side == "l"])

    out = []
    for l in l_docs:
        key = l.get(left_key)
        # A NULL key matches nothing (`NULL = NULL` is not true), so a left row
        # without one is UNMATCHED and belongs in an anti-join's output.
        if key is not None and key in r_keys:
            continue
        out.append(_stitch(l, None, select, l_money, frozenset()))
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
