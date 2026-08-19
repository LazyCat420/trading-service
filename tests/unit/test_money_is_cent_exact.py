"""Cent-exact reconciliation for the money path — the S3 artifact.

A green suite is NOT the evidence this phase requires. Most of the suite reads
money through mocks that never store a Decimal128, so it stays green whether
money reads back as Decimal or as float — it cannot fail on the defect. These
tests run against a REAL MongoDB, store real Decimal128, and reconcile to the
cent.

WHAT IS BEING PROVEN
--------------------
1. Money survives the round trip exactly: what is written is what comes back,
   with no float in between (`test_a_money_column_round_trips_exactly`).
2. The float path FAILS the same reconciliation, so the test can distinguish
   the fixed code from the old code (`test_the_float_path_fails_this_same_check`).
   Without this, "money reconciles" would only mean the tolerance is loose.
3. Non-money columns in money tables stay float, because promoting them was
   the bug that broke the first attempt (`test_ratios_and_counts_stay_float`).
4. The two halves of the contract agree column by column
   (`test_write_and_read_agree_on_every_money_column`).

WHY A LEDGER SUM AND NOT A SINGLE VALUE
---------------------------------------
One value round-tripping proves the codec. It does not prove the ledger, which
is where float error actually shows up: 0.01 is not representable in binary, so
a long run of small credits and debits drifts even though every individual
value "looks right" at two decimal places. The reconciliation below runs 1,000
alternating cash movements — the shape of a trading day — and asserts the
closing balance equals the exact expected total, not that it is close to it.

Run with:  TRADING_BOT_MONGO_TEST=1 pytest tests/unit/test_money_is_cent_exact.py
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import mongo_query
from app.db.table_spec import column_is_money

pytestmark = pytest.mark.real_mongo


# The 7 money collections and their numeric columns, from
# scripts/migration/schema_pg.sql. Written out rather than derived so that a
# ledger regeneration that silently reclassifies a column makes this test fail
# instead of quietly agreeing with itself.
MONEY_TABLE_COLUMNS = {
    "positions": ["qty", "avg_entry_price", "stop_loss_pct", "take_profit_pct"],
    "bots": ["cash_balance", "starting_cash", "total_pnl", "win_rate"],
    "position_lots": ["original_qty", "remaining_qty", "entry_price"],
    "orders": ["qty", "price", "realized_pnl"],
    "trade_fills": ["fill_qty", "fill_price", "fill_value", "fees"],
    "lot_closures": ["closed_qty", "entry_price", "exit_price", "realized_pnl"],
    "portfolio_snapshots": [
        "cash_balance", "total_value", "realized_pnl", "unrealized_pnl",
    ],
}

EXPECTED_NON_MONEY = {
    "positions.qty",
    "positions.stop_loss_pct",
    "positions.take_profit_pct",
    "bots.win_rate",
    "position_lots.original_qty",
    "position_lots.remaining_qty",
    "orders.qty",
    "trade_fills.fill_qty",
    "lot_closures.closed_qty",
}


@pytest.fixture
def money_coll(real_mongo):
    c = real_mongo["bots"]
    c.delete_many({})
    yield c
    c.delete_many({})


def _write_money(coll, doc: dict) -> None:
    """Write through the same codec the backfill and mongo_store use."""
    from app.db.mongo_store import dec128 as to_decimal128

    encoded = {
        k: (to_decimal128(v) if column_is_money("bots", k) else v)
        for k, v in doc.items()
    }
    coll.insert_one(encoded)


# ── 1. the round trip ───────────────────────────────────────────────────────

@pytest.mark.parametrize("amount", [
    "100000.07",      # the value the first attempt was demonstrated on
    "0.01",           # one cent — not representable in binary
    "0.10",
    "12345678.99",    # more digits than a float mantissa holds exactly
    "-4321.05",       # a debit
    "0.00",
])
def test_a_money_column_round_trips_exactly(money_coll, amount):
    """What was written is what comes back — as Decimal, equal exactly."""
    _write_money(money_coll, {"bot_id": "rt", "cash_balance": Decimal(amount)})

    row = mongo_query.find_row("bots", {"bot_id": "rt"}, ["cash_balance"])
    assert row is not None, "the document was written but did not read back"
    (value,) = row

    assert isinstance(value, Decimal), (
        f"cash_balance read back as {type(value).__name__}, not Decimal — the "
        "storage is exact but every sum over it is still float arithmetic"
    )
    assert value == Decimal(amount)
    # `==` on Decimal ignores trailing zeros (0.10 == 0.1); the printed form is
    # what a statement shows, so pin that too.
    assert f"{value:.2f}" == f"{Decimal(amount):.2f}"


# ── 2. the ledger, and the negative control that gives it meaning ───────────

def _ledger_movements() -> list[Decimal]:
    """1,000 cent-scale movements — a trading day's shape, and one that DRIFTS.

    Chosen by measurement, not by eye. A first attempt used strictly
    alternating +/- movements; those cancel, and the float run reconciled
    exactly — `test_the_float_path_fails_this_same_check` caught it and the
    reconciliation would otherwise have been a check that passes for both
    states. Mostly-credit movements with a periodic debit (fees against gains)
    accumulate error: float lands on 100107.63999999978 where the exact answer
    is 100107.64.
    """
    out = []
    for i in range(1, 1001):
        cents = Decimal(str(round(0.01 + (i * 13 % 29) / 100, 2)))
        out.append(-cents if i % 7 == 0 else cents)
    return out


def test_the_ledger_reconciles_to_the_cent(money_coll):
    """1,000 movements, then the closing balance EQUALS the expected total.

    Not `approx`. A tolerance here would pass on exactly the drift this phase
    exists to remove.
    """
    opening = Decimal("100000.07")
    movements = _ledger_movements()
    _write_money(money_coll, {"bot_id": "ledger", "cash_balance": opening})

    balance = opening
    for m in movements:
        # Read → adjust → write, the way the trading path does it, so the value
        # crosses the codec on every iteration rather than staying in Python.
        (current,) = mongo_query.find_row("bots", {"bot_id": "ledger"}, ["cash_balance"])
        balance = current + m
        from app.db.mongo_store import dec128 as to_decimal128

        money_coll.update_one(
            {"bot_id": "ledger"}, {"$set": {"cash_balance": to_decimal128(balance)}}
        )

    (closing,) = mongo_query.find_row("bots", {"bot_id": "ledger"}, ["cash_balance"])
    expected = opening + sum(movements, Decimal(0))

    assert closing == expected, (
        f"ledger drifted: closing {closing} != expected {expected} "
        f"(difference {closing - expected})"
    )
    assert f"{closing:.2f}" == f"{expected:.2f}"


def test_the_float_path_fails_this_same_check():
    """NEGATIVE CONTROL: the pre-fix float path does NOT reconcile.

    Runs the identical movements through float, which is what the code did
    before money read back as Decimal. If this ever passes, the reconciliation
    above is measuring nothing — the movements would be too forgiving to tell
    the two implementations apart, and a regression to float would ship green.
    """
    opening = Decimal("100000.07")
    movements = _ledger_movements()

    balance = float(opening)
    for m in movements:
        balance = balance + float(m)

    expected = opening + sum(movements, Decimal(0))

    assert Decimal(str(balance)) != expected, (
        "the float path reconciled exactly, so these movements cannot "
        "distinguish float from Decimal — pick movements that actually drift"
    )


# ── 3. non-money columns stay float ─────────────────────────────────────────

def test_ratios_and_counts_stay_float(real_mongo):
    """A ratio in a money table must NOT come back as Decimal.

    This is the defect that broke the first attempt: `stop_loss_pct` promoted
    to Decimal, then `entry_price * (1 + tp)` raising TypeError. The column
    being float here is what makes that arithmetic work.
    """
    from app.db.mongo_store import dec128 as to_decimal128

    coll = real_mongo["positions"]
    coll.delete_many({})
    coll.insert_one({
        "bot_id": "mix",
        "avg_entry_price": to_decimal128(Decimal("170.25")),  # money
        "qty": 100.0,                                          # share count
        "stop_loss_pct": 0.08,                                 # ratio
        "take_profit_pct": 0.15,                               # ratio
    })

    row = mongo_query.find_row(
        "positions", {"bot_id": "mix"},
        ["avg_entry_price", "qty", "stop_loss_pct", "take_profit_pct"],
    )
    entry, qty, stop, tp = row

    assert isinstance(entry, Decimal), "the price is money and must be Decimal"
    assert not isinstance(qty, Decimal), "a share count is not money"
    assert not isinstance(stop, Decimal), "a stop-loss ratio is not money"
    assert not isinstance(tp, Decimal), "a take-profit ratio is not money"

    # And the arithmetic that broke the first attempt now works.
    target = entry * (1 + mongo_query.as_money(tp))
    assert target == Decimal("170.25") * Decimal("1.15")

    coll.delete_many({})


# ── 4. the two halves agree ─────────────────────────────────────────────────

def test_write_and_read_agree_on_every_money_column():
    """Both halves resolve through `column_is_money`, so they cannot diverge.

    Checked as a property over every numeric column of every money table
    rather than by re-listing the answers: a test that copies the policy cannot
    see the policy drift.
    """
    classified = {
        f"{table}.{col}"
        for table, cols in MONEY_TABLE_COLUMNS.items()
        for col in cols
        if not column_is_money(table, col)
    }
    assert classified == EXPECTED_NON_MONEY, (
        "the set of non-money columns inside money tables changed.\n"
        f"  now not-money: {sorted(classified)}\n"
        f"  expected     : {sorted(EXPECTED_NON_MONEY)}\n"
        "If this is deliberate, update EXPECTED_NON_MONEY and say why in the "
        "commit — a column silently moving between the two sets changes what "
        "the ledger stores."
    )

    total = sum(len(c) for c in MONEY_TABLE_COLUMNS.values())
    assert total - len(classified) == 17, "expected 17 money columns of 26"


# ── 5. the FIFO sell path, which is where the ledger is actually written ────

@pytest.mark.asyncio
async def test_the_fifo_sell_path_accumulates_pnl_exactly(real_mongo, monkeypatch):
    """`sell()` walks lots and accumulates realized P&L — exactly, now.

    The round trip above proves the codec. This proves the code that USES it:
    the FIFO loop in `paper_trader.sell()` reads `position_lots.entry_price`
    (money) once per lot and adds each lot's P&L to a running total. That total
    is written to `bots.total_pnl` and `orders.realized_pnl`, so it is the
    ledger, and it used to be accumulated in float — `lot_entry =
    float(lot_entry)` — which no codec test can see.

    Deliberately many small lots at cent-scale prices: one lot would reconcile
    under float too, and prove nothing.
    """
    import datetime

    from app.db.mongo_store import dec128
    from app.trading import paper_trader as pt

    bot_id = "fifo-exact"
    ticker = "TESTX"
    now = datetime.datetime.now(datetime.UTC)

    for name in ("bots", "positions", "position_lots", "orders",
                 "trade_fills", "lot_closures", "price_history",
                 "portfolio_snapshots"):
        real_mongo[name].delete_many({})

    # 25, not 200: a 200-lot sell writes 200 closure documents inside one
    # Mongo transaction and the transaction aborts. 25 lots already drift under
    # float (0.02 x 25 sums to 0.4999999999999893, not 0.50), which is what the
    # negative control below pins.
    lot_count = 25
    entry = Decimal("10.07")          # a price that is not exact in binary
    qty_per_lot = 1.0
    exit_price = Decimal("10.09")     # two cents of gain per share
    opening_cash = Decimal("100000.00")

    real_mongo["bots"].insert_one({
        "bot_id": bot_id, "cash_balance": dec128(opening_cash),
        "starting_cash": dec128(opening_cash), "total_pnl": dec128(Decimal("0")),
        "win_rate": 0.0, "total_trades": 0, "is_active": True, "created_at": now,
    })
    real_mongo["positions"].insert_one({
        "id": "pos-fifo", "bot_id": bot_id, "ticker": ticker,
        "qty": float(lot_count) * qty_per_lot,
        "avg_entry_price": dec128(entry), "stop_loss_pct": 0.08,
        "created_at": now, "updated_at": now,
    })
    real_mongo["position_lots"].insert_many([
        {
            "lot_id": f"lot-{i}", "bot_id": bot_id, "ticker": ticker,
            "original_qty": qty_per_lot, "remaining_qty": qty_per_lot,
            "entry_price": dec128(entry), "status": "open",
            "opened_at": now - datetime.timedelta(minutes=lot_count - i),
        }
        for i in range(lot_count)
    ])
    real_mongo["price_history"].insert_one({
        "ticker": ticker, "date": now, "close": float(exit_price),
        "volume": 1_000_000,
    })

    result = await pt.sell(bot_id, ticker, current_price=float(exit_price))
    assert "error" not in result, result

    # The expectation comes from the ACTUAL fill price, not from `exit_price`.
    # `sell()` applies a slippage/fee model, so the fill is a few bps below the
    # reference quote — pinning the frictionless number here would make this
    # test a check on the cost model, which is not its subject. Read it back
    # from the ledger the sell wrote, then assert the SUM over lots is exact.
    fills = mongo_query.find_rows(
        "trade_fills", {"bot_id": bot_id, "ticker": ticker}, ["fill_price"]
    )
    assert len(fills) == 1, f"expected one sell fill, got {len(fills)}"
    (fill_price,) = fills[0]
    assert isinstance(fill_price, Decimal), "fill_price must read back as money"

    per_share_gain = fill_price - entry
    expected_pnl = per_share_gain * Decimal(lot_count) * Decimal(str(qty_per_lot))
    expected_proceeds = fill_price * Decimal(lot_count) * Decimal(str(qty_per_lot))

    (total_pnl, cash) = mongo_query.find_row(
        "bots", {"bot_id": bot_id}, ["total_pnl", "cash_balance"]
    )

    # The point of the test: 25 separate lot P&Ls summed one at a time equal
    # the closed form exactly. Under the old float accumulator they do not.
    assert total_pnl == expected_pnl, (
        f"realized P&L drifted over {lot_count} lots: {total_pnl} != "
        f"{expected_pnl} (difference {total_pnl - expected_pnl})"
    )
    assert cash == opening_cash + expected_proceeds, (
        f"cash balance drifted: {cash} != {opening_cash + expected_proceeds}"
    )
    # And the per-lot closures must reconcile to the same total, so a drift
    # that cancels in the aggregate cannot hide.
    closures = mongo_query.find_rows(
        "lot_closures", {"bot_id": bot_id, "ticker": ticker}, ["realized_pnl"]
    )
    assert len(closures) == lot_count
    assert sum((c[0] for c in closures), Decimal(0)) == expected_pnl

    for name in ("bots", "positions", "position_lots", "orders",
                 "trade_fills", "lot_closures", "price_history",
                 "portfolio_snapshots"):
        real_mongo[name].delete_many({})


def test_the_fifo_sum_is_one_float_cannot_reach():
    """NEGATIVE CONTROL for the FIFO test: float does NOT get that answer.

    Same 200 lots, same arithmetic, in float. If this ever fails, the test
    above cannot tell the fixed accumulator from the old one.
    """
    entry, exit_price, lots = 10.07, 10.09, 25
    float_total = 0.0
    for _ in range(lots):
        float_total += (exit_price - entry) * 1.0

    exact = (Decimal("10.09") - Decimal("10.07")) * Decimal(lots)
    assert Decimal(str(float_total)) != exact, (
        "the float accumulator reached the exact answer, so this lot shape "
        "cannot demonstrate the drift — choose prices that are not exact in "
        "binary"
    )


# ── 6. every read helper, not just the row readers ──────────────────────────

def test_aggregates_over_money_do_not_leak_a_raw_decimal128(real_mongo):
    """`agg_row`/`group_rows`/`join_rows` must unwrap money like `find_rows`.

    `$sum` over a Decimal128 column returns a `bson.Decimal128`, which is not a
    number a caller can compute with and not something `format()` accepts —
    `f"{Decimal128('30.03'):.2f}"` raises TypeError. `strategy_auditor` sums
    `bots.total_pnl` and `bots.cash_balance` exactly this way.

    The row readers were converted and these three were not, so the SAME column
    came back as `Decimal` through `find_rows` and as `Decimal128` through
    `agg_row`. A type that depends on which helper fetched it is the shape of
    bug that survives a green suite.
    """
    from bson import Decimal128

    from app.db.mongo_store import dec128

    coll = real_mongo["bots"]
    coll.delete_many({"bot_id": {"$regex": "^aggmoney"}})
    coll.insert_many([
        {"bot_id": "aggmoney1", "total_pnl": dec128(Decimal("10.01")),
         "cash_balance": dec128(Decimal("100.10")), "win_rate": 50.0},
        {"bot_id": "aggmoney2", "total_pnl": dec128(Decimal("20.02")),
         "cash_balance": dec128(Decimal("200.20")), "win_rate": 25.0},
    ])
    q = {"bot_id": {"$regex": "^aggmoney"}}

    total, avg_rate, count = mongo_query.agg_row(
        "bots", q, [("sum", "total_pnl"), ("avg", "win_rate"), ("count", None)]
    )

    assert not isinstance(total, Decimal128), (
        "agg_row returned a raw bson.Decimal128 — the caller cannot format or "
        "compute with it"
    )
    assert isinstance(total, Decimal), "a sum of money is money"
    assert total == Decimal("30.03"), f"the sum is not exact: {total}"
    # And it is usable where the old float was.
    assert f"{total:.2f}" == "30.03"

    assert not isinstance(avg_rate, Decimal), "win_rate is a ratio, not money"
    assert avg_rate == 37.5
    assert count == 2

    # group_rows: the aggregate AND a money group key.
    rows = mongo_query.group_rows(
        "bots", q, ["bot_id"],
        [("sum", "total_pnl")],
        [("key", "bot_id"), ("agg", 0)],
        sort=[("bot_id", 1)],
    )
    assert len(rows) == 2
    for _, pnl in rows:
        assert not isinstance(pnl, Decimal128), "group_rows leaked a Decimal128"
        assert isinstance(pnl, Decimal)
    assert sum((r[1] for r in rows), Decimal(0)) == Decimal("30.03")

    coll.delete_many({"bot_id": {"$regex": "^aggmoney"}})


def test_join_rows_unwraps_money_on_the_side_it_came_from(real_mongo):
    """A joined money column must not depend on which helper fetched it."""
    from bson import Decimal128

    from app.db.mongo_store import dec128

    real_mongo["positions"].delete_many({"bot_id": "joinmoney"})
    real_mongo["bots"].delete_many({"bot_id": "joinmoney"})
    real_mongo["bots"].insert_one({
        "bot_id": "joinmoney", "cash_balance": dec128(Decimal("500.55")),
        "win_rate": 10.0,
    })
    real_mongo["positions"].insert_one({
        "bot_id": "joinmoney", "ticker": "T",
        "avg_entry_price": dec128(Decimal("10.07")), "qty": 3.0,
    })

    rows = mongo_query.join_rows(
        "positions", {"bot_id": "joinmoney"}, "bot_id",
        "bots", "bot_id",
        left_fields=["ticker", "avg_entry_price", "qty"],
        right_fields=["cash_balance", "win_rate"],
        select=[("l", "avg_entry_price"), ("l", "qty"),
                ("r", "cash_balance"), ("r", "win_rate")],
    )
    assert len(rows) == 1
    entry, qty, cash, rate = rows[0]

    for label, v in (("left money", entry), ("right money", cash)):
        assert not isinstance(v, Decimal128), f"join_rows leaked a Decimal128 ({label})"
        assert isinstance(v, Decimal), f"{label} must read back as Decimal"
    assert entry == Decimal("10.07")
    assert cash == Decimal("500.55")
    assert not isinstance(qty, Decimal), "a share count is not money"
    assert not isinstance(rate, Decimal), "a ratio is not money"

    real_mongo["positions"].delete_many({"bot_id": "joinmoney"})
    real_mongo["bots"].delete_many({"bot_id": "joinmoney"})


def test_as_money_never_routes_through_float():
    """`as_money(0.08)` must be exactly 0.08, not the float's true value.

    `Decimal(0.08)` is 0.080000000000000001665..., which would inject the very
    error the Decimal path exists to remove — at the boundary, where it is
    least visible.
    """
    assert mongo_query.as_money(0.08) == Decimal("0.08")
    assert mongo_query.as_money(0.1) + mongo_query.as_money(0.2) == Decimal("0.3")
    assert mongo_query.as_money(None) is None
    assert mongo_query.as_money(Decimal("1.23")) == Decimal("1.23")
    with pytest.raises(TypeError):
        mongo_query.as_money(True)
