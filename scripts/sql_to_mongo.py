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


def _where(node, ctx: _Ctx) -> dict:
    """Build a Mongo query document from a WHERE tree. Refuses anything whose
    Mongo equivalent is not exact."""
    if node is None:
        return {}
    if isinstance(node, exp.Paren):
        return _where(node.this, ctx)

    if isinstance(node, exp.And):
        left, right = _where(node.left, ctx), _where(node.right, ctx)
        overlap = set(left) & set(right)
        if not overlap:
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


def _translate_select(tree, ctx: _Ctx) -> Translation:
    _reject_hard_features(tree)
    table = _one_table(tree)

    if tree.args.get("distinct"):
        raise Unsupported("SELECT DISTINCT — use distinct_values() by hand")

    # `SELECT count(*) FROM t WHERE ...` is the one aggregate with an exact
    # Mongo equivalent (count_documents). Every other aggregate is refused and
    # computed in Python at the call site.
    if len(tree.expressions) == 1:
        only = tree.expressions[0]
        only = only.this if isinstance(only, exp.Alias) else only
        if isinstance(only, exp.Count) and not only.args.get("distinct"):
            counted = only.this
            if counted is None or isinstance(counted, (exp.Star, exp.Column)):
                q = _where(tree.args["where"].this if tree.args.get("where") else None,
                           ctx)
                if isinstance(counted, exp.Column):
                    # count(col) skips NULLs; count(*) does not.
                    q = {**q, counted.name: {"$ne": None}}
                return Translation("select", table,
                                   f"mongo_query.count({table!r}, {_render(q)})",
                                   ctx.count, "scalar")

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
