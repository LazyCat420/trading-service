"""The money policy: which COLUMNS carry the Decimal128 numeric contract.

ONE FILE, TWO REPOS, BYTE-IDENTICAL. trading-service and trading-client each
carry a copy (they share no runtime code path, only files), and
`scripts/check_shared_files.py` fails the build if they differ. Do not edit one
without the other.

Why the policy is per COLUMN and not per table, which is how it is recorded in
`collection_map.json`: a money collection also carries things that are not
money. Of the 26 numeric columns across the 7 money collections, 9 are ratios
or share counts. Promoting those to Decimal is what made
`entry_price * (1 + take_profit_pct)` raise
`TypeError: unsupported operand type(s) for *: 'Decimal' and 'float'` — on the
RATIO, not on the money — and got the first attempt reverted on 2026-08-18.

Both halves of the contract resolve through `column_is_money()`: the write path
(`_coerce`, and the backfill's mapper) and the read path (`_clean_val` in
`mongo_query`). That symmetry is the point — a column stored as Decimal128 and
read back as float, or the reverse, is the defect this file exists to make
impossible.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# `trade_results` was reclassified OUT of money by ch.64: it holds decision
# PARAMETERS (action, confidence, price levels), not settled amounts, and its
# 1,051 live documents already store them as IEEE floats — honouring the
# ledger's `dec128` would make every existing document disagree with every
# newly written one.
_NUMERIC_OVERRIDES = {
    "trade_results": "float",
}


def _map_numeric_policy(table: str) -> str | None:
    """`numeric_policy` from collection_map.json, if it records one.

    The map outranks any generated source because the map is hand-corrected.
    `bots` is the case that proves it: a classifier called it
    `numeric_policy: "float"`, and a human overrode that with the note "holds
    cash_balance/total_pnl -- money, whatever the classifier says". Reading the
    generated value meant answering False for the account balance while the
    write path was already storing that balance as Decimal128 — the two halves
    of the money contract disagreeing about the one row that holds the cash.
    """
    try:
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
    return _map_numeric_policy(table) == "dec128"


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
# Each entry is a column whose PG type was DOUBLE PRECISION and whose meaning
# is a rate, not a sum. (The schema file this used to name,
# scripts/migration/schema_pg.sql, was deleted at the cutover — it was the
# zombie DDL that resurrected dropped tables. The provenance of these
# entries is the migration ledger and the trading-service copy of this
# policy, which tests/unit/test_money_reads_as_decimal.py holds identical.)
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


# The numeric columns of each money table, as the pre-cutover Postgres schema
# declared them (see the note above: schema_pg.sql itself is gone).
# A money table is mostly NOT money:
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


