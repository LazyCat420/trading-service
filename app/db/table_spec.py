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
(`app/db/migration_ledger.json`) already records for every table in scope —
the 183 the manifest declares, plus the live tables it never saw.

What this module deliberately does NOT do: guess renames or defaults. The
hand-written `pipeline_events` mapper renames `data_json` -> `data` and floors
`elapsed_ms` to 0; nothing in the schema says so. Tables needing that keep an
explicit override in `TABLES`, and `tests/unit/test_table_spec_generator.py`
pins which tables agree with the generator and which genuinely differ — so a
divergence is a listed fact rather than a surprise at backfill time.
"""

from __future__ import annotations

import json
from pathlib import Path
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Callable

from bson import Decimal128

_LEDGER_PATH = os.path.join(os.path.dirname(__file__), "migration_ledger.json")


def quote_ident(name: str) -> str:
    """Quote a Postgres identifier for interpolation into generated SQL.

    Every identifier this module emits comes from information_schema, so it is
    already the real, lowercase name -- quoting is behaviour-preserving. It is
    not optional, though: `cycle_schedules` has a column literally named
    `analyze`, which Postgres classifies as fully reserved, so the unquoted
    `SELECT id, ..., collect, analyze, trade, ... FROM cycle_schedules` is a
    syntax error. That one table aborted the entire `--verify-fields all` sweep
    for every other table behind it.

    Embedded double quotes are doubled per the Postgres rule. A name containing
    a NUL cannot be an identifier at all and is rejected rather than truncated.
    """
    if "\x00" in name:
        raise ValueError(f"identifier contains a NUL byte: {name!r}")
    return '"' + name.replace('"', '""') + '"'

# Postgres types that arrive as text but are documents.
_JSON_TYPES = {"json", "jsonb"}
# Types that need a value transform on the way into BSON.
_VECTOR_UDT = "vector"

# The ledger is GENERATED (scripts/build_migration_ledger.py), so it cannot be
# hand-corrected — a regeneration would silently undo the edit. These are the
# facts the classifier gets wrong, recorded here with why, until the classifier
# itself is fixed.
_KEY_OVERRIDES = {
    # The classifier reads the `id` column and stops. The mirror is really keyed
    # on request_id: the ch.62 heal re-keyed it that way after 31,637 id-less
    # documents turned up, and the unique index follows request_id. Keying a
    # backfill on `id` here would mirror rows alongside the existing documents
    # instead of onto them, doubling the collection.
    "agent_audit_log": "request_id",
}
_NUMERIC_OVERRIDES = {
    # ch.64 established that this table holds DECISION PARAMETERS (action,
    # confidence, reasoning, price levels) and not settled amounts — no cash, no
    # quantities, no realized P&L — and recommended reclassifying it out of
    # `money`. Its 1,051 live documents already store these as IEEE floats, so
    # honouring the ledger's `dec128` would make every existing document
    # disagree with every newly written one.
    "trade_results": "float",
}

_ledger_cache: dict[str, dict] | None = None


def _ledger() -> dict[str, dict]:
    """Per-table ledger rows, by table name."""
    global _ledger_cache
    if _ledger_cache is None:
        with open(_LEDGER_PATH) as fh:
            data = json.load(fh)
        _ledger_cache = {row["table"]: row for row in data.get("tables", [])}
    return _ledger_cache


def key_fields_for(table: str) -> list[str]:
    """The column(s) identifying a row, as a list — 1 for most tables, 2-3 for 26.

    Composite keys are the rule for the biggest tables, not an exception:
    `price_history` is (ticker, date, source), `technicals` is (ticker, date),
    `sec_13f_holdings` is (cik, ticker, filing_quarter). Keying on any single
    column of those collapses every row that shares it.
    """
    row = _ledger().get(table)
    if row is None:
        raise KeyError(f"{table!r} is not in migration_ledger.json — regenerate it "
                       "(scripts/build_migration_ledger.py) before migrating this table")
    if table in _KEY_OVERRIDES:
        return [_KEY_OVERRIDES[table]]
    raw = (row.get("natural_key") or row.get("key_field") or "").strip()
    if not raw:
        raise ValueError(f"{table!r} has no natural_key or key_field in the ledger")
    return [part.strip() for part in raw.split(",") if part.strip()]


def key_field_for(table: str) -> str:
    """The single column that identifies a row in both stores.

    Prefers the ledger's `natural_key`, falling back to `key_field`. Raises on a
    composite key rather than silently keying on part of it: the backfill's
    keyset pagination and the verifier's `$in` lookups both take one column, and
    a spec that quietly dropped the rest would mirror rows on top of each other
    and report the collapse as parity.

    **26 of the 161 migrate tables are composite** — including the three biggest
    (`price_history` on `ticker, date, source`, `technicals` on `ticker, date`,
    `sec_13f_holdings` on `cik, ticker, filing_quarter`), so this one serves the
    other 135.

    Composite keys ARE supported now — by `key_fields_for()` above, and by the
    backfill's keyset pagination (`_paginate_clause`). This function is the
    single-column accessor for callers that genuinely take one column; it raises
    on a composite rather than keying on part of it, which is still the point:
    loudly unsupported beats quietly wrong. Prefer `key_fields_for()` in new
    code.
    """
    row = _ledger().get(table)
    if row is None:
        raise KeyError(f"{table!r} is not in migration_ledger.json — regenerate it "
                       "(scripts/build_migration_ledger.py) before migrating this table")
    if table in _KEY_OVERRIDES:
        return _KEY_OVERRIDES[table]
    key = (row.get("natural_key") or row.get("key_field") or "").strip()
    if not key:
        raise ValueError(f"{table!r} has no natural_key or key_field in the ledger")
    if "," in key:
        raise ValueError(
            f"{table!r} has a COMPOSITE key ({key!r}), which this generator does "
            "not serve yet (26 of 158 tables, including price_history, technicals "
            "and sec_13f_holdings). Keying on one of its columns would collapse "
            "rows that differ only in the others and report it as parity. Add an "
            "explicit entry to TABLES, or add composite-key support to the "
            "backfill's pagination and the verifier's lookups."
        )
    return key


def _map_numeric_policy(table: str) -> str | None:
    """`numeric_policy` from collection_map.json, if it records one.

    The map outranks the ledger because the ledger is GENERATED and the map is
    hand-corrected. `bots` is the case that proves it: the classifier called it
    `numeric_policy: "float", shape: "mutable"`, and a human overrode that in
    the map with the note "holds cash_balance/total_pnl -- money, whatever the
    classifier says". Reading only the ledger meant this function answered
    False for the account balance while paper_trader was already writing that
    balance through dec128 — the two halves of the money contract disagreeing
    about the one row that holds the cash.
    """
    try:
        import json

        path = Path(__file__).with_name("collection_map.json")
        entry = json.loads(path.read_text(encoding="utf-8"))["collections"].get(table)
        if isinstance(entry, dict):
            return entry.get("numeric_policy")
    except Exception:  # noqa: BLE001 - a missing map must not break callers
        pass
    return None


def uses_decimal128(table: str) -> bool:
    """True when this table's numbers are money and must be Decimal128."""
    if table in _NUMERIC_OVERRIDES:
        return _NUMERIC_OVERRIDES[table] == "dec128"
    mapped = _map_numeric_policy(table)
    if mapped is not None:
        return mapped == "dec128"
    row = _ledger().get(table)
    return bool(row and row.get("numeric_policy") == "dec128")


# Columns that sit inside a money table but are NOT money, by exact
# `table.column`. The numeric policy is recorded per TABLE, which is one
# granularity too coarse: of the 25 numeric columns across the 7 money
# collections, three are not settled amounts, and promoting them to Decimal is
# what produced `TypeError: unsupported operand type(s) for *: 'Decimal' and
# 'float'` at paper_trader.py:1173 — `entry_price * (1 + effective_tp)`, where
# the entry price is money and the target is a ratio.
#
# A ratio is not money: it is multiplied by money, compared against float
# quotes from vendors, and fed to numpy and pandas. Making it Decimal buys no
# exactness (there are no cents in 0.08) and costs a TypeError at every
# boundary. Share counts are excluded for the same reason — `qty` is a
# multiplicand, not an amount, and it is float on both sides of the migration.
#
# Each entry is a column whose PG type is DOUBLE PRECISION in
# scripts/migration/schema_pg.sql (or added by pg_migrations.py) and whose
# meaning is a rate, not a sum.
_NON_MONEY_COLUMNS: frozenset[str] = frozenset({
    # Risk parameters — fractions of a price, e.g. 0.08 == 8%.
    "positions.stop_loss_pct",
    "positions.take_profit_pct",   # added by pg_migrations.py:88
    # A rate in [0, 1], not an amount.
    "bots.win_rate",
})

# Share counts. Money tables carry quantities alongside amounts, and a count
# multiplied by a price is money — but the count itself is not, and it meets
# float prices, numpy and pandas everywhere. Listed separately from the ratios
# above only so the reason stays legible.
_QUANTITY_COLUMNS: frozenset[str] = frozenset({
    "positions.qty",
    "orders.qty",
    "position_lots.original_qty",
    "position_lots.remaining_qty",
    "trade_fills.fill_qty",
    "lot_closures.closed_qty",
})


# The numeric columns of each money table, from scripts/migration/schema_pg.sql
# plus the columns pg_migrations.py adds. A money table is mostly NOT money:
# `bots` carries a bot_id, a name and timestamps alongside its four numbers.
#
# This is an allow-list rather than a deny-list because the two directions fail
# differently. Asking "is this column excluded?" answers True for `ticker` and
# `created_at` — every text and timestamp column in a money table — and a
# caller that trusts the name then hands a string to Decimal128 and gets
# `InvalidOperation`. Asking "is this column one of the known amounts?" answers
# False for anything unrecognised, which is the safe direction: a new column is
# float until someone classifies it, and a float that should have been money is
# a precision bug, while a string that becomes money is a crash.
_MONEY_COLUMNS: frozenset[str] = frozenset({
    "positions.avg_entry_price",
    "bots.cash_balance", "bots.starting_cash", "bots.total_pnl",
    "position_lots.entry_price",
    "orders.price", "orders.realized_pnl",
    "trade_fills.fill_price", "trade_fills.fill_value", "trade_fills.fees",
    "lot_closures.entry_price", "lot_closures.exit_price",
    "lot_closures.realized_pnl",
    "portfolio_snapshots.cash_balance", "portfolio_snapshots.total_value",
    "portfolio_snapshots.realized_pnl", "portfolio_snapshots.unrealized_pnl",
})


def column_is_money(table: str, column: str) -> bool:
    """True when THIS column must be Decimal128, not just its table.

    Read by both halves of the money contract — `_coerce()` on the write side
    and `mongo_query._clean_val()` on the read side — so a column cannot be
    stored as Decimal128 and read back as float, or vice versa. That symmetry
    is the point: the two sides disagreeing about one column is the defect this
    function exists to make impossible.

    Gated on `uses_decimal128(table)` as well as membership, so a table
    reclassified out of money (as `trade_results` was, ch.64) takes its columns
    with it and cannot be re-promoted by a stale entry here.
    """
    if not uses_decimal128(table):
        return False
    qualified = f"{table}.{column}"
    if qualified in _NON_MONEY_COLUMNS or qualified in _QUANTITY_COLUMNS:
        return False
    return qualified in _MONEY_COLUMNS


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
    # BSON has no `date` type: pymongo raises InvalidDocument on a bare
    # datetime.date, which is what psycopg returns for a `date` column. There
    # are 26 such columns, and several composite keys are built on them
    # (price_history ticker,date,source · technicals ticker,date ·
    # put_call_ratio symbol,date), so this is not a corner. Store midnight UTC,
    # which sorts and range-queries the same way the date did.
    # NOTE the isinstance order: datetime SUBCLASSES date, so a plain
    # `isinstance(value, date)` would rewrite every timestamp in the database.
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
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


def spec_for(table: str, db) -> tuple[str, list[str], Callable]:
    """(select_sql, key_fields, mapper) for `table`, derived from the schema.

    `key_fields` is always a LIST, single-column or not, so every caller handles
    one shape. The SELECT names its columns explicitly rather than using `*` so
    the document shape is pinned to what this spec was built from: a column
    added later changes the mirror only when the spec is regenerated
    deliberately.
    """
    keys = key_fields_for(table)
    cols = columns_for(table, db)
    names = [c[0] for c in cols]
    missing = [k for k in keys if k not in names]
    if missing:
        raise ValueError(
            f"key column(s) {missing} are not columns of {table!r} (has: {', '.join(names)})"
        )

    # Per COLUMN, not per table: a money table also carries ratios and share
    # counts, and storing those as Decimal128 makes every read that multiplies
    # them by a float quote raise TypeError. See `column_is_money`.
    money_cols = {name: column_is_money(table, name) for name in names}
    types = {name: (dt, udt) for name, dt, udt in cols}
    select_sql = f"SELECT {', '.join(quote_ident(n) for n in names)} FROM {quote_ident(table)}"

    def _mapper(row, row_cols):
        d = dict(zip(row_cols, row))
        return {
            name: _coerce(d.get(name), *types.get(name, ("text", "text")),
                          money_cols.get(name, False))
            for name in row_cols
        }

    return select_sql, keys, _mapper
