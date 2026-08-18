"""Lot closures on the SELL path: FIFO matching, closure rows, realised P&L.

These used to seed a real Postgres test database (`real_db`) and patch
`paper_trader.get_db`, then read `lot_closures` back with SQL. paper_trader
calls `mongo_query`/`mongo_store` now — `get_db` is not a symbol it imports, so
the patch raised, and the whole file was skipped whenever TRADING_BOT_TEST_DB
was unset (which is always, in CI). Nothing here was measuring anything.

Rewritten against the Mongo layer. The invariants are unchanged and the
assertions are STRONGER: instead of re-reading a table they read the exact
document the money path handed `insert_docs`, so a closure written to the wrong
collection, keyed to the wrong lot, or carrying the wrong price would fail here
rather than round-trip through a schema that would accept it.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.trading.paper_trader import sell


def _zero_execution_cost(ticker, price, side, notional):
    """Identity fill: no spread, impact or commission.

    These tests pin LOT BOOKKEEPING — FIFO matching, closure rows, realised
    P&L arithmetic — not the execution-cost model. With the live cost model
    the fill lands below the reference price (open item 39's 159.36 vs 160.0:
    the assertions were written before costs shipped on 2026-07-26 and assert
    gross-of-cost numbers). Zeroing the cost here keeps the expectations exact
    and deterministic; the cost model has its own coverage.
    """
    return price, {
        "total_bps": 0.0, "spread_bps": 0.0, "impact_bps": 0.0,
        "commission_bps": 0.0, "commission_cash": 0.0,
        "spread_source": "test-zero", "fully_modeled": True,
    }


class _MongoDouble:
    """Collection-keyed stand-in for paper_trader's mongo_query + mongo_store.

    `find_row`/`find_rows` return TUPLES in the column order the caller
    requested — the positional contract of `app/db/mongo_query.py`. Handing
    back documents would exercise a shape the module never sees.
    """

    def __init__(self, position, open_lots):
        self._position = position
        self._open_lots = open_lots
        self.query = MagicMock()
        self.store = MagicMock()

        self.query.find_row.side_effect = self._find_row
        self.query.find_rows.side_effect = self._find_rows
        self.query.agg_row.side_effect = lambda *_a, **_k: (None,)

        self.store.with_txn.side_effect = self._with_txn
        # Value-PRESERVING dec128 so the monetary assertions below read the
        # number the money path actually stored. A bare MagicMock would hide
        # every price and P&L behind an opaque sentinel and the arithmetic
        # assertions would silently stop testing arithmetic.
        self.store.dec128.side_effect = lambda v: v
        # Branch predicates: a bare MagicMock is truthy for BOTH writes_mongo
        # and writes_pg, which silently exercises the dual-write path.
        self.store.writes_mongo.side_effect = lambda _t: True
        self.store.writes_pg.side_effect = lambda _t: False
        self.store.reads_mongo.side_effect = lambda _t: True
        # win_rate recompute reads lot_closures back through find_docs (DICTS,
        # not tuples — mongo_store's reads are document-shaped).
        self.store.find_docs.side_effect = self._find_docs

    def _find_row(self, collection, query, columns, **kwargs):
        if collection == "trade_fills":
            return None                      # no duplicate SELL in cycle
        if collection == "positions":
            return self._position            # (id, qty, avg_entry_price)
        if collection == "price_history":
            return (160.0, None)             # latest close, date
        if collection == "bots":
            return ("test-bot",)
        return None

    def _find_rows(self, collection, query, columns, **kwargs):
        if collection == "position_lots":
            # (lot_id, remaining_qty, entry_price, opened_at), oldest first —
            # sort=[('opened_at', 1)] is the FIFO order the matcher relies on.
            return list(self._open_lots)
        return []

    def _find_docs(self, collection, query, **kwargs):
        if collection == "lot_closures":
            return [{"realized_pnl": d["realized_pnl"]} for d in self.inserted("lot_closures")]
        return []

    @contextmanager
    def _with_txn(self):
        yield "session-sentinel"

    def inserted(self, collection):
        out = []
        for call in self.store.insert_docs.call_args_list:
            if call[0][0] == collection:
                out.extend(call[0][1])
        return out

    def updates(self, collection):
        """(filter, update) for every update issued against `collection`."""
        return [
            (c[0][1], c[0][2]) for c in self.store.update_docs.call_args_list
            if c[0][0] == collection
        ]

    def deletes(self, collection):
        return [c[0][1] for c in self.store.delete_docs.call_args_list if c[0][0] == collection]


def _patch(double):
    return [
        patch("app.trading.paper_trader.mongo_query", double.query),
        patch("app.trading.paper_trader.mongo_store", double.store),
        patch("app.trading.paper_trader._ensure_bot", lambda *_a, **_k: None),
        patch("app.trading.paper_trader._apply_execution_cost", new=_zero_execution_cost),
        patch("app.trading.paper_trader._record_portfolio_snapshot", MagicMock()),
    ]


async def _sell(double, **kwargs):
    patches = _patch(double)
    for p in patches:
        p.start()
    try:
        return await sell("test-bot", "AAPL", **kwargs)
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_lot_closures_created_on_sell():
    """A full SELL must create exactly one lot_closures document per lot consumed."""
    double = _MongoDouble(
        position=("pos-123", 10.0, 150.0),
        open_lots=[("lot-abc", 10.0, 150.0, None)],
    )

    result = await _sell(double, qty_pct=1.0)

    assert result.get("error") is None, f"Sell failed: {result.get('error')}"
    assert result["action"] == "SELL"
    assert result["qty"] == 10.0
    # 10 shares bought at 150, sold at 160 → $100 realised, gross of cost.
    assert result["realized_pnl"] == 100.0

    closures = double.inserted("lot_closures")
    assert len(closures) == 1, "Expected one insert into lot_closures"

    c = closures[0]
    assert c["bot_id"] == "test-bot"
    assert c["ticker"] == "AAPL"
    assert c["closed_qty"] == 10.0
    assert c["entry_price"] == 150.0
    assert c["exit_price"] == 160.0
    assert c["realized_pnl"] == 100.0
    # The closure must name the lot it consumed and the sell fill that
    # consumed it — that pairing is what makes the ledger auditable.
    assert c["lot_id"] == "lot-abc"
    fills = double.inserted("trade_fills")
    assert len(fills) == 1
    assert c["sell_fill_id"] == fills[0]["fill_id"]

    # The lot is fully consumed, so it closes out at zero remaining.
    assert double.updates("position_lots") == [
        ({"lot_id": "lot-abc"}, {"$set": {"remaining_qty": 0.0, "status": "closed"}})
    ]
    # A full close removes the position rather than leaving a zero-qty row.
    assert double.deletes("positions") == [{"id": "pos-123"}]

    # Cash and realised P&L land on the bot's own row, in one $inc.
    bots_incs = [u for u in double.updates("bots") if "$inc" in u[1]]
    assert len(bots_incs) == 1
    filt, upd = bots_incs[0]
    assert filt == {"bot_id": "test-bot"}
    assert upd["$inc"]["cash_balance"] == pytest.approx(1600.0)   # 10 @ 160
    assert upd["$inc"]["total_pnl"] == pytest.approx(100.0)
    assert upd["$inc"]["total_trades"] == 1

    # Every ledger write rode inside the ONE transaction: a closure committed
    # outside the session could survive a rolled-back position delete.
    for call in double.store.insert_docs.call_args_list:
        assert call.kwargs.get("session") == "session-sentinel"


@pytest.mark.asyncio
async def test_lot_closures_partial_sell():
    """A partial SELL closes only the sold amount; the lot stays 'partial'."""
    double = _MongoDouble(
        position=("pos-123", 10.0, 150.0),
        open_lots=[("lot-abc", 10.0, 150.0, None)],
    )

    result = await _sell(double, qty_pct=0.5)

    assert result.get("error") is None
    assert result["qty"] == 5.0

    closures = double.inserted("lot_closures")
    assert len(closures) == 1
    c = closures[0]
    assert c["closed_qty"] == 5.0        # closed_qty is only 5.0
    assert c["realized_pnl"] == 50.0     # realized_pnl is only 50.0
    assert c["entry_price"] == 150.0
    assert c["exit_price"] == 160.0

    # Half the lot survives, and it must remain matchable for the NEXT sell:
    # status 'partial' is in the open-lot query's $in, 'closed' is not.
    assert double.updates("position_lots") == [
        ({"lot_id": "lot-abc"}, {"$set": {"remaining_qty": 5.0, "status": "partial"}})
    ]
    # The position is reduced, not deleted, and keeps its 150.0 cost basis —
    # the surviving lot is what the new average is recomputed from.
    assert double.deletes("positions") == []
    pos_updates = double.updates("positions")
    assert len(pos_updates) == 1
    filt, upd = pos_updates[0]
    assert filt == {"id": "pos-123"}
    assert upd["$set"]["qty"] == pytest.approx(5.0)
    assert upd["$set"]["avg_entry_price"] == pytest.approx(150.0)


@pytest.mark.asyncio
async def test_fifo_consumes_the_oldest_lot_first():
    """FIFO: the oldest lot is matched first, and its entry price — not the
    position's blended average — sets the realised P&L.

    The original pair of tests only ever held ONE lot, so a matcher that
    consumed lots in any order, or that priced every closure off
    avg_entry_price, would have passed both.
    """
    double = _MongoDouble(
        # Blended average of 10 @ 100 and 10 @ 200.
        position=("pos-123", 20.0, 150.0),
        open_lots=[
            ("lot-old", 10.0, 100.0, None),
            ("lot-new", 10.0, 200.0, None),
        ],
    )

    result = await _sell(double, qty_pct=0.5)   # sell 10 of 20

    assert result["qty"] == 10.0
    closures = double.inserted("lot_closures")
    assert len(closures) == 1, "10 shares must come out of the oldest lot alone"
    c = closures[0]
    assert c["lot_id"] == "lot-old"
    assert c["entry_price"] == 100.0
    # (160 - 100) * 10 = 600 — priced off the LOT, not the 150.0 average
    # (which would have given 100).
    assert c["realized_pnl"] == pytest.approx(600.0)
    assert result["realized_pnl"] == pytest.approx(600.0)

    # The untouched newer lot must not be restated.
    assert double.updates("position_lots") == [
        ({"lot_id": "lot-old"}, {"$set": {"remaining_qty": 0.0, "status": "closed"}})
    ]
    # The surviving position's cost basis is rebuilt from the REMAINING lots,
    # so it moves from the 150.0 blend to the 200.0 lot that is left.
    filt, upd = double.updates("positions")[0]
    assert upd["$set"]["avg_entry_price"] == pytest.approx(200.0)
