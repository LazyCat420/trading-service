"""Generate a table's Postgres→Mongo migration spec instead of hand-writing it.

`scripts/pg_to_mongo_backfill.py` carried one hand-written entry per table — an
explicit SELECT column list, a key field, and a document mapper. That is fine
for a dozen tables and impossible for the 149 still to move: it is 149 more
triples to write, review and keep in step with the schema.

It does not need to be hand-written. Measured across all 214 live tables, only
eight columns in the entire database are anything other than a scalar, a
timestamp or JSON:

    3 x `vector`  (pgvector: embeddings, user_data, ontology_nodes)
    5 x `text[]`  (morning_briefings, flash_briefings, whiteboard_entries,
                   decision_scores x2)

against 106 json/jsonb columns and ~2,000 plain scalars. So the mapping is
derivable from `information_schema` plus the key and numeric policy the ledger
(`app/db/migration_ledger.json`) already records for all 183 manifest tables.

What this module deliberately does NOT do: guess renames or defaults. The
hand-written `pipeline_events` mapper renames `data_json` -> `data` and floors
`elapsed_ms` to 0; nothing in the schema says so. Tables needing that keep an
explicit override in `TABLES`, and `tests/unit/test_table_spec_generator.py`
pins which tables agree with the generator and which genuinely differ — so a
divergence is a listed fact rather than a surprise at backfill time.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Callable

from bson import Decimal128

_LEDGER_PATH = os.path.join(os.path.dirname(__file__), "migration_ledger.json")

# Postgres types that arrive as text but are documents.
_JSON_TYPES = {"json", "jsonb"}
# Types that need a value transform on the way into BSON.
_VECTOR_UDT = "vector"

_ledger_cache: dict[str, dict] | None = None


def _ledger() -> dict[str, dict]:
    """Per-table ledger rows, by table name."""
    global _ledger_cache
    if _ledger_cache is None:
        with open(_LEDGER_PATH) as fh:
            data = json.load(fh)
        _ledger_cache = {row["table"]: row for row in data.get("tables", [])}
    return _ledger_cache


def key_field_for(table: str) -> str:
    """The single column that identifies a row in both stores.

    Prefers the ledger's `natural_key`, falling back to `key_field`. Raises on a
    composite key rather than silently keying on half of it: the backfill's
    keyset pagination and the verifier's `$in` lookups both need one column, and
    a spec that quietly dropped the second column would mirror rows on top of
    each other. Only 2 of 158 tables are composite; they take an override.
    """
    row = _ledger().get(table)
    if row is None:
        raise KeyError(f"{table!r} is not in migration_ledger.json — regenerate it "
                       "(scripts/build_migration_ledger.py) before migrating this table")
    key = (row.get("natural_key") or row.get("key_field") or "").strip()
    if not key:
        raise ValueError(f"{table!r} has no natural_key or key_field in the ledger")
    if "," in key:
        raise ValueError(
            f"{table!r} has a COMPOSITE key ({key!r}). Add an explicit entry to "
            "TABLES in scripts/pg_to_mongo_backfill.py — a generated spec would "
            "key on one column and collapse rows that differ only in the other."
        )
    return key


def uses_decimal128(table: str) -> bool:
    """True when the ledger classifies this table's numbers as money."""
    row = _ledger().get(table)
    return bool(row and row.get("numeric_policy") == "dec128")


def columns_for(table: str, db) -> list[tuple[str, str, str]]:
    """(name, data_type, udt_name) in ordinal order, from the live schema."""
    cur = db.execute(
        "SELECT column_name, data_type, udt_name "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position",
        [table],
    )
    cols = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    if not cols:
        raise KeyError(f"{table!r} has no columns in information_schema — "
                       "does it exist in this database?")
    return cols


def _coerce(value, data_type: str, udt_name: str, money: bool):
    if value is None:
        return None
    if data_type in _JSON_TYPES:
        # psycopg returns jsonb as a dict already; json/text columns holding
        # JSON arrive as str. A string that does not parse is kept verbatim
        # rather than silently replaced with {} — losing the original text is
        # worse than storing it.
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return value
        return value
    if udt_name == _VECTOR_UDT:
        return list(value) if value is not None else None
    if money and isinstance(value, (Decimal, float, int)) and not isinstance(value, bool):
        return Decimal128(Decimal(str(value)))
    if isinstance(value, Decimal):
        return float(value)
    return value


def spec_for(table: str, db) -> tuple[str, str, Callable]:
    """(select_sql, key_field, mapper) for `table`, derived from the schema.

    The SELECT names its columns explicitly rather than using `*` so the
    document shape is pinned to what this spec was built from: a column added
    later changes the mirror only when the spec is regenerated deliberately.
    """
    key = key_field_for(table)
    cols = columns_for(table, db)
    names = [c[0] for c in cols]
    if key not in names:
        raise ValueError(f"key {key!r} is not a column of {table!r} (has: {', '.join(names)})")

    money = uses_decimal128(table)
    types = {name: (dt, udt) for name, dt, udt in cols}
    select_sql = f"SELECT {', '.join(names)} FROM {table}"

    def _mapper(row, row_cols):
        d = dict(zip(row_cols, row))
        return {
            name: _coerce(d.get(name), *types.get(name, ("text", "text")), money)
            for name in row_cols
        }

    return select_sql, key, _mapper
