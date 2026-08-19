#!/usr/bin/env python3
"""Translate a single SQL statement into an equivalent mongo_store call.

Pure function, no database, no filesystem: `translate(sql)` takes SQL text and
returns a Translation describing the Mongo call that replaces it, or explains
why it refuses. The codemod uses it to rewrite call sites; the tests use it to
prove the rewrites are right.

DESIGN RULE — refuse, never approximate.
Every construct this module does not implement EXACTLY raises Unsupported with
a reason. There is no best-effort path and no partial translation, because a
query that returns plausible-but-wrong rows is far worse than one that fails to
convert: the first ships and corrupts decisions, the second shows up as a
refusal in a report you can read. `%` in a LIKE, a correlated subquery, an
`ORDER BY` over an expression — all refusals.

Placeholders: Postgres `%s` params are positional. They map onto the Python
argument list in order of appearance, so the generated call reuses the caller's
existing parameter expressions untouched, in the same order. A statement whose
`%s` count disagrees with the caller's argument count is refused rather than
guessed at.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp


class Unsupported(Exception):
    """This statement is not mechanically translatable. Carries the reason."""


@dataclass
class Translation:
    verb: str                       # select | insert | update | delete
    table: str
    call: str                       # python expression template, {p0}.. for params
    n_params: int                   # how many %s the statement carried
    returns: str                    # rows | one | count | none
    notes: list[str] = field(default_factory=list)


# Operators we can express in a Mongo query document with identical semantics.
_CMP = {
    exp.EQ: None,          # {field: value}
    exp.NEQ: "$ne",
    exp.GT: "$gt",
    exp.GTE: "$gte",
    exp.LT: "$lt",
    exp.LTE: "$lte",
}


def _placeholder_stream():
    """Yields '{p0}', '{p1}', ... in the order %s appears in the statement."""
    i = 0
    while True:
        yield "{p%d}" % i
        i += 1


class _Ctx:
    def __init__(self):
        self._n = 0

    def next_param(self) -> str:
        tok = "{p%d}" % self._n
        self._n += 1
        return tok

    @property
    def count(self) -> int:
        return self._n


def _column(node) -> str:
    if isinstance(node, exp.Column):
        return node.name
    raise Unsupported(f"expected a plain column, got {type(node).__name__}")


_INTERVAL_UNITS = {
    "second": "seconds", "seconds": "seconds",
    "minute": "minutes", "minutes": "minutes",
    "hour": "hours", "hours": "hours",
    "day": "days", "days": "days",
    "week": "weeks", "weeks": "weeks",
}


def _interval(node) -> str:
    """`INTERVAL '7 days'` → `timedelta(days=7)`. Months and years are refused:
    they are not fixed-length, so timedelta cannot express them exactly."""
    text = node.this.this if isinstance(node.this, exp.Literal) else str(node.this)
    unit = (node.args.get("unit").name.lower()
            if node.args.get("unit") is not None else "")
    m = re.fullmatch(r"\s*(\d+)\s*([a-z]*)\s*", str(text).lower())
    if not m:
        raise Unsupported(f"INTERVAL {text!r} is not a plain <n> <unit>")
    n, inline_unit = m.group(1), m.group(2)
    unit = (inline_unit or unit).lower()
    if unit in ("month", "months", "year", "years"):
        raise Unsupported(f"INTERVAL in {unit} — not a fixed length, "
                          "timedelta cannot express it exactly")
    if unit not in _INTERVAL_UNITS:
        raise Unsupported(f"INTERVAL unit {unit!r}")
    return f"timedelta({_INTERVAL_UNITS[unit]}={n})"


def _value(node, ctx: _Ctx) -> str:
    """Render a SQL value node as Python source."""
    if isinstance(node, exp.Placeholder) or (
        isinstance(node, exp.Parameter)
    ):
        return ctx.next_param()
    if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDate)):
        # Mongo stores real datetimes; the Postgres server clock becomes the
        # application clock. Always tz-aware — a naive datetime compares
        # wrongly against the tz-aware values already in these collections.
        return "datetime.now(timezone.utc)"
    if isinstance(node, exp.Interval):
        return _interval(node)
    if isinstance(node, (exp.Add, exp.Sub)):
        # Date arithmetic: NOW() - INTERVAL '7 days'
        left, right = _value(node.left, ctx), _value(node.right, ctx)
        op = "+" if isinstance(node, exp.Add) else "-"
        return f"({left} {op} {right})"
    if isinstance(node, exp.Literal):
        if node.is_string:
            return repr(node.this)
        text = node.name
        return text if re.fullmatch(r"-?\d+(\.\d+)?", text) else repr(text)
    if isinstance(node, exp.Boolean):
        return "True" if node.this else "False"
    if isinstance(node, exp.Null):
        return "None"
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return "-" + _value(node.this, ctx)
    raise Unsupported(f"value {type(node).__name__} is not a literal or placeholder")


def _and_merge(left: dict, right: dict) -> dict:
    """AND two query documents.

    Shared by the WHERE builder and the JOIN splitter, which has to merge the
    predicates it sorted onto one side. Duplicating this was the alternative,
    and a copied merge cannot see the original drift.
    """
    if not set(left) & set(right):
        return {**left, **right}
    # Same field constrained twice (a BETWEEN-style range) — merge only when
    # both sides are operator maps, else the second would silently win.
    merged = dict(left)
    for k, v in right.items():
        if k in merged:
            if isinstance(merged[k], dict) and isinstance(v, dict) and not (
                set(merged[k]) & set(v)
            ):
                merged[k] = {**merged[k], **v}
            else:
                raise Unsupported(f"conflicting conditions on {k!r}")
        else:
            merged[k] = v
    return merged


def _where(node, ctx: _Ctx) -> dict:
    """Build a Mongo query document from a WHERE tree. Refuses anything whose
    Mongo equivalent is not exact."""
    if node is None:
        return {}
    if isinstance(node, exp.Paren):
        return _where(node.this, ctx)

    if isinstance(node, exp.And):
        return _and_merge(_where(node.left, ctx), _where(node.right, ctx))

    if isinstance(node, exp.Or):
        return {"$or": [_where(node.left, ctx), _where(node.right, ctx)]}

    if isinstance(node, exp.Not):
        inner = node.this
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            return {_column(inner.this): {"$ne": None}}
        if isinstance(inner, exp.In):
            return {_column(inner.this): {"$nin": _in_list(inner, ctx)}}
        raise Unsupported("NOT over a non-trivial expression")

    if isinstance(node, exp.Is):
        if isinstance(node.expression, exp.Null):
            # sqlglot encodes `IS NOT NULL` as Is(negate=True), NOT as
            # Not(Is(...)). Ignoring the flag turned every IS NOT NULL into
            # IS NULL — a silent inversion that returned the complement of the
            # intended rows. Caught by the differential check, never by parsing.
            if node.args.get("negate"):
                return {_column(node.this): {"$ne": None}}
            return {_column(node.this): None}
        raise Unsupported("IS over a non-NULL expression")

    if isinstance(node, exp.In):
        return {_column(node.this): {"$in": _in_list(node, ctx)}}

    for cls, op in _CMP.items():
        if isinstance(node, cls):
            col, val = node.left, node.right
            if isinstance(col, exp.Literal) and isinstance(val, exp.Column):
                col, val = val, col          # `5 < x` → `x > 5`
                op = {"$gt": "$lt", "$lt": "$gt",
                      "$gte": "$lte", "$lte": "$gte"}.get(op, op)
            name = _column(col)
            rendered = _value(val, ctx)
            return {name: rendered if op is None else {op: rendered}}

    if isinstance(node, exp.Between):
        name = _column(node.this)
        return {name: {"$gte": _value(node.args["low"], ctx),
                       "$lte": _value(node.args["high"], ctx)}}

    if isinstance(node, exp.Like):
        raise Unsupported("LIKE — Mongo $regex has different escaping semantics")
    if isinstance(node, exp.Boolean):
        # `WHERE TRUE` style guard
        return {} if node.this else {"_id": {"$exists": False}}
    if isinstance(node, exp.Column):
        # a bare boolean column
        return {node.name: True}

    raise Unsupported(f"WHERE construct {type(node).__name__}")


def _in_list(node, ctx: _Ctx) -> str:
    exprs = node.expressions
    if not exprs:
        raise Unsupported("empty IN list")
    if len(exprs) == 1 and isinstance(exprs[0], (exp.Select, exp.Subquery)):
        raise Unsupported("IN (subquery)")
    return "[" + ", ".join(_value(e, ctx) for e in exprs) + "]"


def _render(doc) -> str:
    """Render a query/update document as Python source (values already source)."""
    if isinstance(doc, dict):
        inner = ", ".join(f"{k!r}: {_render(v)}" for k, v in doc.items())
        return "{" + inner + "}"
    if isinstance(doc, list):
        return "[" + ", ".join(_render(v) for v in doc) + "]"
    return str(doc)


def _one_table(tree) -> str:
    tables = {t.name for t in tree.find_all(exp.Table) if t.name}
    if len(tables) != 1:
        raise Unsupported(f"expects exactly one table, found {sorted(tables)}")
    return tables.pop()


def _reject_hard_features(tree) -> None:
    for cls, why in (
        (exp.Join, "JOIN"),
        (exp.Group, "GROUP BY"),
        (exp.With, "CTE"),
        (exp.Window, "window function"),
        (exp.Union, "UNION"),
        (exp.Subquery, "subquery"),
        (exp.Having, "HAVING"),
    ):
        if list(tree.find_all(cls)):
            raise Unsupported(why)


_AGG_NODES = {exp.Count: "count", exp.Min: "min", exp.Max: "max",
              exp.Avg: "avg", exp.Sum: "sum"}


def _agg_list(tree) -> list[tuple[str, str | None]] | None:
    """[(op, field)] when the SELECT list is ENTIRELY plain aggregates.

    Returns None if any item is not an aggregate — a mixed list like
    `SELECT ticker, COUNT(*)` is an implicit GROUP BY and is not this shape.
    Refuses (raises) on an aggregate whose Mongo form would not be exact, so a
    conditional SUM nobody implemented cannot silently become a plain SUM.
    """
    if tree.args.get("group") or tree.args.get("having"):
        return None
    out: list[tuple[str, str | None]] = []
    for e in tree.expressions:
        node = e.this if isinstance(e, exp.Alias) else e
        cls = type(node)
        if cls not in _AGG_NODES:
            return None
        op = _AGG_NODES[cls]
        inner = node.this

        if isinstance(node, exp.Count):
            if node.args.get("distinct") or isinstance(inner, exp.Distinct):
                col = inner.expressions[0] if isinstance(inner, exp.Distinct) else inner
                if not isinstance(col, exp.Column):
                    raise Unsupported("COUNT(DISTINCT <expression>)")
                out.append(("count_distinct", col.name))
                continue
            if inner is None or isinstance(inner, exp.Star):
                out.append(("count", None))
                continue
            if isinstance(inner, exp.Column):
                out.append(("count", inner.name))
                continue
            raise Unsupported("COUNT over an expression")

        # SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END) — the null-counting idiom.
        if isinstance(node, exp.Sum) and isinstance(inner, exp.Case):
            ifs = inner.args.get("ifs") or []
            if len(ifs) == 1:
                cond = ifs[0].this
                if (isinstance(cond, exp.Is) and isinstance(cond.expression, exp.Null)
                        and not cond.args.get("negate")
                        and isinstance(cond.this, exp.Column)):
                    out.append(("count_null", cond.this.name))
                    continue
            raise Unsupported("SUM(CASE ...) other than the IS NULL idiom")

        if not isinstance(inner, exp.Column):
            raise Unsupported(f"{op.upper()} over an expression")
        out.append((op, inner.name))
    return out or None


def _translate_group_by(tree, table, ctx: _Ctx) -> Translation:
    """`SELECT k, COUNT(*) ... GROUP BY k` -> mongo_query.group_rows().

    Refuses HAVING (a post-group filter needs a $match after $group, which is
    expressible but is a different shape and nothing in this codebase uses it),
    grouping over an expression, and any SELECT item that is neither a grouped
    key nor an aggregate — Postgres rejects those too, so seeing one means the
    statement was not what it looked like.
    """
    if tree.args.get("having"):
        raise Unsupported("GROUP BY ... HAVING")
    keys = []
    for g in tree.args["group"].expressions:
        if not isinstance(g, exp.Column):
            raise Unsupported("GROUP BY over an expression")
        keys.append(g.name)

    select: list[tuple[str, object]] = []
    aggs: list[tuple[str, str | None]] = []
    alias_of: dict[str, str] = {}     # SELECT alias -> pipeline field name
    for e in tree.expressions:
        node = e.this if isinstance(e, exp.Alias) else e
        alias = e.alias if isinstance(e, exp.Alias) else None
        if isinstance(node, exp.Column):
            if node.name not in keys:
                raise Unsupported(f"{node.name!r} is selected but not grouped")
            select.append(("key", node.name))
            continue
        one = _agg_list(_FakeSelect([node]))
        if one is None:
            raise Unsupported(f"SELECT item {type(node).__name__} in a GROUP BY")
        aggs.extend(one)
        select.append(("agg", len(aggs) - 1))
        if alias:
            alias_of[alias] = f"a{len(aggs) - 1}"

    q = _where(tree.args["where"].this if tree.args.get("where") else None, ctx)

    sort = None
    if tree.args.get("order"):
        pairs = []
        for o in tree.args["order"].expressions:
            col = o.this
            if not isinstance(col, exp.Column):
                raise Unsupported("ORDER BY over an expression in a GROUP BY")
            name = col.name
            if name in alias_of:
                # `ORDER BY n DESC` where n aliases COUNT(*): the pipeline
                # field is a0, not n. Passing the alias through sorted on a
                # field that does not exist — a silent no-op that returns the
                # rows in $group order and looks like it worked.
                name = alias_of[name]
            elif name not in keys:
                raise Unsupported(
                    f"ORDER BY {name!r} is neither a grouped key nor a "
                    "SELECT alias")
            pairs.append(f"({name!r}, {-1 if o.args.get('desc') else 1})")
        sort = "[" + ", ".join(pairs) + "]"

    args = [f"{table!r}", _render(q),
            "[" + ", ".join(repr(k) for k in keys) + "]",
            "[" + ", ".join(f"({op!r}, {f!r})" for op, f in aggs) + "]",
            "[" + ", ".join(f"({k!r}, {v!r})" for k, v in select) + "]"]
    if sort:
        args.append(f"sort={sort}")
    if tree.args.get("limit"):
        args.append(f"limit={_value(tree.args['limit'].expression, ctx)}")
    return Translation("select", table,
                       f"mongo_query.group_rows({', '.join(args)})",
                       ctx.count, "rows")


class _FakeSelect:
    """Minimal stand-in so _agg_list can classify a single SELECT item."""

    def __init__(self, expressions):
        self.expressions = expressions
        self.args = {}


def _and_leaves(node) -> list:
    """Flatten a top-level AND chain, left to right — i.e. in SQL order."""
    if isinstance(node, exp.Paren):
        return _and_leaves(node.this)
    if isinstance(node, exp.And):
        return _and_leaves(node.left) + _and_leaves(node.right)
    return [node]


def _translate_join(tree, ctx: _Ctx) -> Translation:
    """A two-table INNER JOIN on one equality -> mongo_query.join_rows().

    Deliberately narrow, because join_rows() is an INNER join done as two
    queries and a Python stitch. Anything whose result set differs from that
    shape is refused BY NAME rather than approximated:

    - LEFT/RIGHT/FULL keep non-matching rows and an inner stitch drops them,
      which silently turns a reporting query into a filter. Worse,
      `LEFT JOIN ... WHERE right.col IS NULL` is an ANTI-join: translated as an
      inner join it returns the exact COMPLEMENT of the intended rows. That is
      the same shape as the `IS NOT NULL` inversion the differential checker
      caught, and it is why LEFT is refused here instead of being approximated
      with a flag.
    - ORDER BY may only name LEFT-side columns. join_rows sorts the left
      collection and then stitches, so a sort key on the right table would be
      accepted and silently ignored.
    - Unqualified columns are refused: without the schema there is no way to
      tell which side owns one, and guessing wrong picks a field that does not
      exist, which Mongo answers with null rather than an error.
    """
    joins = list(tree.find_all(exp.Join))
    if len(joins) != 1:
        raise Unsupported(
            f"{len(joins)} JOINs — join_rows() joins exactly two tables")
    join = joins[0]

    side = (join.side or "").upper()
    if side:
        raise Unsupported(
            f"{side} JOIN — join_rows() is an INNER join, which drops the "
            f"non-matching rows a {side} JOIN keeps")
    kind = (join.kind or "").upper()
    if kind and kind != "INNER":
        raise Unsupported(f"{kind} JOIN")

    for cls, why in ((exp.Group, "GROUP BY"), (exp.Having, "HAVING"),
                     (exp.With, "CTE"), (exp.Window, "window function"),
                     (exp.Union, "UNION"), (exp.Subquery, "subquery")):
        if list(tree.find_all(cls)):
            raise Unsupported(f"{why} together with a JOIN")
    if tree.args.get("distinct"):
        raise Unsupported("SELECT DISTINCT with a JOIN")

    # sqlglot 30 renamed this arg to `from_`; accept both so a version bump
    # cannot silently turn every JOIN into "derived table" and refuse it.
    from_node = tree.args.get("from_") or tree.args.get("from")
    left_node = from_node.this if from_node is not None else None
    right_node = join.this
    if not isinstance(left_node, exp.Table) or not isinstance(right_node, exp.Table):
        raise Unsupported("JOIN over a derived table")
    left_table, right_table = left_node.name, right_node.name
    left_alias = left_node.alias or left_table
    right_alias = right_node.alias or right_table
    if left_alias == right_alias:
        raise Unsupported("self-join — both sides carry the same name")

    def side_of(col) -> str:
        q = col.table
        if not q:
            raise Unsupported(
                f"unqualified column {col.name!r} in a JOIN — which table owns "
                "it is not decidable from the statement")
        if q == left_alias:
            return "l"
        if q == right_alias:
            return "r"
        raise Unsupported(f"unknown table qualifier {q!r}")

    on = join.args.get("on")
    if on is None:
        raise Unsupported("JOIN without ON")
    if not isinstance(on, exp.EQ):
        raise Unsupported("JOIN ON is not a single equality")
    a, b = on.left, on.right
    if not (isinstance(a, exp.Column) and isinstance(b, exp.Column)):
        raise Unsupported("JOIN ON compares something other than two columns")
    if side_of(a) == side_of(b):
        raise Unsupported("JOIN ON names the same table twice")
    left_key = a.name if side_of(a) == "l" else b.name
    right_key = b.name if side_of(b) == "r" else a.name

    select: list[tuple[str, str]] = []
    for e in tree.expressions:
        node = e.this if isinstance(e, exp.Alias) else e
        if isinstance(node, exp.Star) or node.find(exp.Star) is not None:
            raise Unsupported("SELECT * in a JOIN")
        if not isinstance(node, exp.Column):
            raise Unsupported(f"SELECT item {type(node).__name__} in a JOIN")
        select.append((side_of(node), node.name))
    if not select:
        raise Unsupported("JOIN with no selected columns")

    # Placeholders are numbered in the order next_param() is CALLED, so the
    # WHERE leaves must be walked in SQL order and bucketed afterwards. Building
    # one side's query fully and then the other would renumber every parameter
    # that crosses the split -- a mis-binding no parse or type check can see.
    left_q: dict = {}
    right_q: dict = {}
    where = tree.args.get("where")
    if where is not None:
        for leaf in _and_leaves(where.this):
            sides = {side_of(c) for c in leaf.find_all(exp.Column)}
            if len(sides) != 1:
                raise Unsupported(
                    "a WHERE condition spans both tables — that is a join "
                    "predicate, not a filter, and join_rows() takes one key")
            doc = _where(leaf, ctx)
            if sides == {"l"}:
                left_q = _and_merge(left_q, doc)
            else:
                right_q = _and_merge(right_q, doc)

    sort = None
    if tree.args.get("order"):
        pairs = []
        for o in tree.args["order"].expressions:
            col = o.this
            if not isinstance(col, exp.Column):
                raise Unsupported("ORDER BY over an expression in a JOIN")
            if side_of(col) != "l":
                raise Unsupported(
                    "ORDER BY names the joined table — join_rows() sorts the "
                    "left collection only, so this sort would be dropped")
            pairs.append(f"({col.name!r}, {-1 if o.args.get('desc') else 1})")
        sort = "[" + ", ".join(pairs) + "]"

    def _uniq(items):
        out = []
        for x in items:
            if x not in out:
                out.append(x)
        return out

    left_fields = _uniq([c for s, c in select if s == "l"])
    right_fields = _uniq([c for s, c in select if s == "r"])

    args = [repr(left_table), _render(left_q), repr(left_key),
            repr(right_table), repr(right_key)]
    if right_q:
        args.append(f"right_query={_render(right_q)}")
    args.append("left_fields=[" + ", ".join(repr(f) for f in left_fields) + "]")
    args.append("right_fields=[" + ", ".join(repr(f) for f in right_fields) + "]")
    args.append("select=[" + ", ".join(f"({s!r}, {c!r})" for s, c in select) + "]")
    if sort:
        args.append(f"sort={sort}")
    if tree.args.get("limit"):
        args.append(f"limit={_value(tree.args['limit'].expression, ctx)}")

    return Translation("select", left_table,
                       f"mongo_query.join_rows({', '.join(args)})",
                       ctx.count, "rows")


def _translate_select(tree, ctx: _Ctx) -> Translation:
    # GROUP BY is handled, so it must be checked BEFORE the blanket rejection —
    # _reject_hard_features lists exp.Group and was vetoing every grouped
    # statement before the branch that knows how to translate it could run.
    if tree.args.get("group") and not list(tree.find_all(exp.Join)):
        return _translate_group_by(tree, _one_table(tree), ctx)
    # Same reason, same trap: _reject_hard_features lists exp.Join, so this
    # must come first or the branch that knows how to translate a JOIN can
    # never run. _translate_join refuses the shapes it cannot express, and its
    # reasons are more specific than a blanket "JOIN".
    if list(tree.find_all(exp.Join)) and not tree.args.get("group"):
        return _translate_join(tree, ctx)
    _reject_hard_features(tree)
    table = _one_table(tree)

    if tree.args.get("distinct"):
        raise Unsupported("SELECT DISTINCT — use distinct_values() by hand")


    # Single-table, no-GROUP-BY aggregate list: `SELECT COUNT(*), MIN(d), MAX(d)`.
    # Exactly expressible as one $group, so it becomes a single agg_row() call
    # rather than a fetch-and-reduce in Python — several of these run over
    # price_history, where fetching to count would be far slower than the SQL.
    aggs = _agg_list(tree)
    if aggs is not None:
        q = _where(tree.args["where"].this if tree.args.get("where") else None, ctx)
        spec = "[" + ", ".join(f"({op!r}, {fld!r})" for op, fld in aggs) + "]"
        return Translation("select", table,
                           f"mongo_query.agg_row({table!r}, {_render(q)}, {spec})",
                           ctx.count, "row")

    projection = None
    fields = []
    star = False
    literal_only = True
    for e in tree.expressions:
        node = e.this if isinstance(e, exp.Alias) else e
        if isinstance(node, exp.Star):
            star = True
            literal_only = False
        elif isinstance(node, exp.Column):
            fields.append(node.name)
            literal_only = False
        elif isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            raise Unsupported("aggregate in SELECT list")
        elif isinstance(node, exp.Literal):
            # `SELECT 1 FROM t WHERE ...` — an existence probe. The value is
            # never read; only whether a row came back. Project nothing.
            continue
        else:
            # A computed column (`NOW() - x`, a CASE, a cast). Postgres names it
            # `?column?` when unaliased and the caller reads it positionally;
            # Mongo has no such field, so the row would silently lose it.
            raise Unsupported(
                f"SELECT item {type(node).__name__} is a computed column — "
                "compute it in Python after the find_docs()"
            )
    if not star and fields:
        projection = {f: 1 for f in fields}
        projection["_id"] = 0
    elif literal_only and not star:
        projection = {"_id": 1}

    query = _where(tree.args.get("where").this if tree.args.get("where") else None, ctx)

    sort = None
    if tree.args.get("order"):
        pairs = []
        for o in tree.args["order"].expressions:
            if not isinstance(o.this, exp.Column):
                raise Unsupported("ORDER BY over an expression")
            pairs.append(f"({o.this.name!r}, {-1 if o.args.get('desc') else 1})")
        sort = "[" + ", ".join(pairs) + "]"

    limit = None
    if tree.args.get("limit"):
        limit = _value(tree.args["limit"].expression, ctx)
        # find_docs() treats limit=0 as "no limit" (the pymongo convention),
        # while SQL LIMIT 0 means "no rows". Passing it through returned the
        # whole collection where the caller asked for nothing.
        if limit == "0":
            raise Unsupported("LIMIT 0 — pymongo reads 0 as unlimited, the "
                              "inverse of SQL; write the empty result by hand")
    if tree.args.get("offset"):
        raise Unsupported("OFFSET — find_docs has no skip parameter")

    # Emit a ROW-shaped call. Application code indexes results positionally
    # (`r[0]`), so a dict-returning find_docs() would break every caller; the
    # rewrite is only mechanical if the replacement keeps the shape.
    args = [f"{table!r}", _render(query)]
    if fields:
        args.append("[" + ", ".join(repr(f) for f in fields) + "]")
        fn = "find_rows"
    elif star:
        args.append("")   # placeholder, removed below
        args.pop()
        fn = "find_dicts"
    else:
        fn = "exists"     # SELECT <literal> — used as a boolean probe
    if sort:
        args.append(f"sort={sort}")
    if limit and fn != "exists":
        args.append(f"limit={limit}")
    call = f"mongo_query.{fn}({', '.join(args)})"
    notes = []
    if star:
        notes.append("SELECT * returns documents, not tuples — a caller that "
                     "unpacks positionally must name its columns first")
    return Translation("select", table, call, ctx.count,
                       "rows" if fn != "exists" else "bool", notes)


def _translate_insert(tree, ctx: _Ctx) -> Translation:
    table = _one_table(tree)
    schema = tree.this
    if not isinstance(schema, exp.Schema) or not schema.expressions:
        raise Unsupported("INSERT without an explicit column list")
    cols = [c.name for c in schema.expressions]

    values = tree.expression
    if isinstance(values, exp.Select):
        raise Unsupported("INSERT ... SELECT")
    if not isinstance(values, exp.Values) or len(values.expressions) != 1:
        raise Unsupported("INSERT with multiple VALUES tuples")
    tup = values.expressions[0]
    if len(tup.expressions) != len(cols):
        raise Unsupported("column/value count mismatch")
    doc = {c: _value(v, ctx) for c, v in zip(cols, tup.expressions)}

    conflict = tree.args.get("conflict")
    if conflict is None:
        call = f"mongo_store.insert_docs({table!r}, [{_render(doc)}])"
        return Translation("insert", table, call, ctx.count, "none")

    # ON CONFLICT — the key is the conflict target, the rest is the update.
    target = conflict.args.get("conflict_keys") or conflict.args.get("conflict_target")
    keys = []
    if target:
        items = target if isinstance(target, list) else getattr(target, "expressions", [])
        for k in items:
            keys.append(k.name if hasattr(k, "name") else str(k))
    if not keys:
        raise Unsupported("ON CONFLICT without an explicit target")
    key_doc = {k: doc[k] for k in keys if k in doc}
    if len(key_doc) != len(keys):
        raise Unsupported("ON CONFLICT target is not among the inserted columns")

    # `action` is a Var node ("DO NOTHING" / "DO UPDATE"), never a bare string.
    # Comparing it to "NOTHING" silently matched nothing, so every DO NOTHING
    # was translated as an overwriting $set — the exact opposite of its meaning.
    action = conflict.args.get("action")
    action_name = (getattr(action, "name", None) or str(action or "")).upper()

    if "NOTHING" in action_name:
        call = (f"mongo_store.upsert_doc({table!r}, {_render(key_doc)}, "
                f"{_render(doc)}, insert_only=True)")
        return Translation("insert", table, call, ctx.count, "none",
                           ["ON CONFLICT DO NOTHING → insert_only=True"])

    if "UPDATE" not in action_name:
        raise Unsupported(f"ON CONFLICT action {action_name!r}")

    # DO UPDATE SET — translate the actual SET list. `EXCLUDED.col` means the
    # value this statement tried to insert, which is doc[col].
    sets: dict[str, str] = {}
    for assign in conflict.args.get("expressions") or []:
        if not isinstance(assign, exp.EQ):
            raise Unsupported("ON CONFLICT SET item is not a simple assignment")
        target = _column(assign.left)
        rhs = assign.right
        if isinstance(rhs, exp.Column) and (rhs.table or "").upper() == "EXCLUDED":
            if rhs.name not in doc:
                raise Unsupported(f"EXCLUDED.{rhs.name} is not an inserted column")
            sets[target] = doc[rhs.name]
        else:
            try:
                sets[target] = _value(rhs, ctx)
            except Unsupported as exc:
                raise Unsupported(f"ON CONFLICT SET {target}: {exc}") from exc
    if not sets:
        raise Unsupported("ON CONFLICT DO UPDATE with an empty SET list")

    # $setOnInsert carries the columns the SET list does not touch, so a NEW
    # document is still complete while an EXISTING one only gets the SET fields
    # — which is what ON CONFLICT DO UPDATE actually does. upsert_doc() cannot
    # express both halves, so emit the driver call directly.
    on_insert = {k: v for k, v in doc.items() if k not in sets and k not in key_doc}
    update_parts = [f"'$set': {_render(sets)}"]
    if on_insert:
        update_parts.append(f"'$setOnInsert': {_render(on_insert)}")
    call = (f"mongo_store.update_docs({table!r}, {_render(key_doc)}, "
            f"{{{', '.join(update_parts)}}}, upsert=True)")
    return Translation("insert", table, call, ctx.count, "none",
                       ["ON CONFLICT DO UPDATE → $set of the SET list only"])


def _translate_update(tree, ctx: _Ctx) -> Translation:
    _reject_hard_features(tree)
    table = _one_table(tree)
    if tree.args.get("from"):
        raise Unsupported("UPDATE ... FROM")
    sets = {}
    for e in tree.expressions:
        if not isinstance(e, exp.EQ):
            raise Unsupported("SET item is not a simple assignment")
        sets[_column(e.left)] = _value(e.right, ctx)
    query = _where(tree.args.get("where").this if tree.args.get("where") else None, ctx)
    call = (f"mongo_store.update_docs({table!r}, {_render(query)}, "
            f"{{'$set': {_render(sets)}}})")
    return Translation("update", table, call, ctx.count, "count")


def _translate_delete(tree, ctx: _Ctx) -> Translation:
    _reject_hard_features(tree)
    table = _one_table(tree)
    query = _where(tree.args.get("where").this if tree.args.get("where") else None, ctx)
    if not query:
        raise Unsupported("DELETE with no WHERE — refusing a whole-collection wipe")
    call = f"mongo_store.delete_docs({table!r}, {_render(query)})"
    return Translation("delete", table, call, ctx.count, "count")


# Postgres system catalogs and information_schema views. These describe the
# DATABASE, not the application's data, and Mongo has no equivalent — so a
# translation of them is not a port, it is a silently wrong answer.
#
# Found on 2026-08-19 in trading-client: the codemod happily rewrote
#
#     SELECT tablename FROM pg_tables WHERE schemaname = 'public'
#  -> mongo_query.find_rows('pg_tables', {'schemaname': 'public'}, ['tablename'])
#
# at 10 call sites. Valid code, no error, and `pg_tables` is not a collection —
# so "list the tables in this database" would return an empty list forever, and
# every caller that iterates the result would simply do nothing. The routes
# affected are the schema browser, the ontology builder and the data-audit
# sweep, all of which would report a clean, empty database.
#
# The right port is `db.list_collection_names()` (or `$listCatalog`), which is
# a hand transform, not a query rewrite — hence a refusal rather than a
# translation.
_SYSTEM_CATALOGS = frozenset({
    "pg_tables", "pg_class", "pg_attribute", "pg_indexes", "pg_stat_user_tables",
    "pg_stat_activity", "pg_namespace", "pg_type", "pg_constraint",
    "pg_stat_statements", "pg_locks", "pg_settings", "pg_database",
    # `information_schema.columns` arrives from sqlglot as the bare name
    # `columns`, which reads like an ordinary table and is the most dangerous
    # of these — it is also a plausible application table name.
    "columns", "tables", "key_column_usage", "table_constraints",
    "referential_constraints", "information_schema",
})


def _refuse_system_catalogs(tree) -> None:
    """Refuse any statement touching a Postgres catalog. See _SYSTEM_CATALOGS."""
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        db = (table.text("db") or "").lower()
        if db == "information_schema" or name in _SYSTEM_CATALOGS:
            qualified = f"{db}.{name}" if db else name
            raise Unsupported(
                f"{qualified} is a Postgres system catalog — Mongo has no "
                "equivalent, and translating it yields a query against a "
                "collection that does not exist (a silent empty result). Use "
                "db.list_collection_names() by hand."
            )


def translate(sql: str) -> Translation:
    """SQL text → Translation. Raises Unsupported with a reason."""
    norm = " ".join(sql.split())
    if not norm:
        raise Unsupported("empty statement")
    if "RETURNING" in norm.upper():
        raise Unsupported("RETURNING — Mongo needs find_one_and_update by hand")
    # sqlglot reads %s as a modulo operator; make it a real placeholder first.
    prepared = norm.replace("%s", ":_p")
    try:
        tree = sqlglot.parse_one(prepared, dialect="postgres")
    except Exception as exc:
        raise Unsupported(f"unparsed: {exc}") from exc
    if tree is None:
        raise Unsupported("unparsed: empty tree")

    _refuse_system_catalogs(tree)

    ctx = _Ctx()
    if isinstance(tree, exp.Select):
        t = _translate_select(tree, ctx)
    elif isinstance(tree, exp.Insert):
        t = _translate_insert(tree, ctx)
    elif isinstance(tree, exp.Update):
        t = _translate_update(tree, ctx)
    elif isinstance(tree, exp.Delete):
        t = _translate_delete(tree, ctx)
    else:
        raise Unsupported(f"statement type {type(tree).__name__}")

    expected = norm.count("%s")
    if t.n_params != expected:
        raise Unsupported(
            f"placeholder count mismatch: statement has {expected} %s, "
            f"translation consumed {t.n_params}"
        )
    return t


if __name__ == "__main__":
    import sys
    for stmt in sys.argv[1:]:
        try:
            t = translate(stmt)
            print(f"OK   {t.verb:<7} {t.table:<22} {t.call}")
        except Unsupported as exc:
            print(f"SKIP {exc}")
