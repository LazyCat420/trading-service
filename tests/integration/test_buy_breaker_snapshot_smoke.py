"""
Integration smoke: drawdown breaker + snapshot writer on the live BUY path
(plan item D4).

In an all-HOLD regime neither the drawdown breaker nor the post-trade snapshot
writer ever fires live, because both hang off an executed BUY. Rather than mock
the full orchestrator/decision stack to force a `confidence=65` trade, this
drives the real `paper_trader.buy()` execution path directly — the single place
where `_check_drawdown_breaker` (gate) and `_record_portfolio_snapshot` (writer)
actually fire together — and proves the wiring:

  * a seeded peak that puts the book past the drawdown limit BLOCKS the BUY
    (no trade_fills document written), with the REAL breaker running; and
  * a healthy book lets the BUY through and fires the snapshot writer afterward.

The reads are dispatched on the COLLECTION NAME, not on call order and not on
SQL text — buy() issues several ordered reads (dup-check, cash, price sanity,
positions, then the breaker's peak read) and keying on the collection keeps the
test robust if that order changes.

This file used to patch `paper_trader.get_db` and key a fake cursor on SQL
substrings. paper_trader calls `mongo_query`/`mongo_store` now, so `get_db` was
not even importable there: the patch raised, and before the autouse Mongo guard
landed the fake cursor intercepted nothing. Marked `integration` because it
exercises the whole execution path, not one function.
"""
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.trading import paper_trader

pytestmark = pytest.mark.integration


class _MongoDouble:
    """Collection-keyed stand-in for paper_trader's mongo_query + mongo_store.

    Reads answer by collection name and return TUPLES in the column order the
    caller asked for — that positional contract is what `app/db/mongo_query.py`
    guarantees, so a fixture returning documents would be testing a shape the
    module never sees.
    """

    def __init__(self, cash, peak):
        self.cash = cash
        self.peak = peak
        self.query = MagicMock()
        self.store = MagicMock()

        self.query.find_row.side_effect = self._find_row
        self.query.find_rows.side_effect = self._find_rows
        self.query.agg_row.side_effect = self._agg_row

        self.store.with_txn.side_effect = self._with_txn
        # dec128 is value-PRESERVING here so monetary assertions can read the
        # number the money path actually stored. A bare MagicMock would swallow
        # every amount behind an opaque sentinel.
        self.store.dec128.side_effect = lambda v: v
        # writes_mongo/writes_pg are BRANCH PREDICATES. A bare MagicMock is
        # truthy for both, silently exercising the dual-write path.
        self.store.writes_mongo.side_effect = lambda _t: True
        self.store.writes_pg.side_effect = lambda _t: False
        self.store.reads_mongo.side_effect = lambda _t: True
        self.store.find_docs.side_effect = lambda *_a, **_k: []

    # ── reads ──
    def _find_row(self, collection, query, columns, **kwargs):
        if collection == "trade_fills":
            return None            # no duplicate BUY in cycle
        if collection == "bots":
            return (self.cash,) if columns == ["cash_balance"] else ("bot-1",)
        if collection == "positions":
            return None            # no existing position → INSERT branch
        if collection == "price_history":
            return None            # no history → skip the price-sanity gate
        return None

    def _find_rows(self, collection, query, columns, **kwargs):
        if collection == "positions":
            return []
        return []

    def _agg_row(self, collection, query, aggs, **kwargs):
        if collection == "portfolio_snapshots":
            return (self.peak,)
        if collection == "price_history":
            return (None,)         # no 30d average → sanity gate skipped
        return (None,)

    # ── transaction plumbing ──
    @contextmanager
    def _with_txn(self):
        yield "session-sentinel"

    # ── write inspection ──
    def inserted(self, collection):
        """Every document inserted into `collection`."""
        out = []
        for call in self.store.insert_docs.call_args_list:
            if call[0][0] == collection:
                out.extend(call[0][1])
        return out


def _patch_buy(double):
    """Patch the Mongo layer + the external helpers buy() reaches, leaving the
    real breaker and the real execution/gating logic intact."""
    return [
        patch("app.trading.paper_trader.mongo_query", double.query),
        patch("app.trading.paper_trader.mongo_store", double.store),
        patch("app.trading.paper_trader._ensure_bot", lambda *_a, **_k: None),
        patch("app.trading.paper_trader._compute_stop_loss_pct", return_value=0.08),
    ]


@pytest.mark.asyncio
async def test_breaker_blocks_buy_on_the_live_path():
    # Book is 50k against a 100k peak → -50% drawdown, past the 25% limit.
    double = _MongoDouble(cash=50_000.0, peak=100_000.0)
    patches = _patch_buy(double)
    for p in patches:
        p.start()
    try:
        with patch("app.trading.paper_trader.get_param", lambda k: 0.25):
            result = await paper_trader.buy("bot-1", "AAPL", size_pct=0.10, current_price=150.0)
    finally:
        for p in patches:
            p.stop()

    # Real breaker fired → BUY refused with the breaker's error, and nothing
    # was written to the broker ledger.
    assert "error" in result
    assert "drawdown breaker" in result["error"].lower()
    assert result["reason_code"] == "DRAWDOWN_BREAKER"
    assert double.inserted("trade_fills") == [], "a blocked BUY must not write a fill"
    assert double.inserted("position_lots") == [], "a blocked BUY must not open a lot"
    assert double.inserted("positions") == [], "a blocked BUY must not open a position"
    # A refused BUY must not touch the cash ledger either.
    assert double.store.update_docs.call_args_list == []


@pytest.mark.asyncio
async def test_healthy_book_executes_and_snapshots():
    # Book is at its 100k peak → 0% drawdown → breaker allows the BUY.
    double = _MongoDouble(cash=100_000.0, peak=100_000.0)
    patches = _patch_buy(double)
    for p in patches:
        p.start()
    snap = MagicMock()
    snap_patch = patch("app.trading.paper_trader._record_portfolio_snapshot", snap)
    snap_patch.start()
    try:
        # MAX_PORTFOLIO_DRAWDOWN_PCT gates the breaker; MAX_CONCENTRATION_PCT
        # gates the per-ticker cap. Both resolve through the parameter store.
        params = {"MAX_PORTFOLIO_DRAWDOWN_PCT": 0.25, "MAX_CONCENTRATION_PCT": 0.25}
        with patch("app.trading.paper_trader.get_param", lambda k: params[k]):
            result = await paper_trader.buy("bot-1", "AAPL", size_pct=0.10, current_price=150.0)
    finally:
        snap_patch.stop()
        for p in patches:
            p.stop()

    # BUY went through the real path...
    assert result.get("action") == "BUY", f"expected a BUY, got {result}"

    # ...and wrote exactly one fill for the intended notional. 10% of a 100k
    # book = $10,000; the fill lands at or ABOVE the $150 reference because
    # execution costs make a BUY fill WORSE than the reference price.
    fills = double.inserted("trade_fills")
    assert len(fills) == 1, "a cleared BUY must write exactly one fill"
    fill = fills[0]
    assert fill["side"] == "BUY"
    assert fill["ticker"] == "AAPL"
    assert fill["bot_id"] == "bot-1"
    assert fill["fill_value"] == pytest.approx(10_000.0)
    assert fill["decision_price"] == 150.0
    assert fill["fill_price"] >= 150.0, "a BUY must not fill better than the reference"
    assert fill["fill_qty"] == pytest.approx(10_000.0 / fill["fill_price"])

    # The lot that the FIFO matcher will later consume, and the position.
    lots = double.inserted("position_lots")
    assert len(lots) == 1
    assert lots[0]["fill_id"] == fill["fill_id"]
    assert lots[0]["remaining_qty"] == lots[0]["original_qty"] == fill["fill_qty"]
    assert lots[0]["status"] == "open"

    positions = double.inserted("positions")
    assert len(positions) == 1
    assert positions[0]["ticker"] == "AAPL"
    assert positions[0]["qty"] == pytest.approx(fill["fill_qty"])
    assert positions[0]["avg_entry_price"] == fill["fill_price"]

    # Cash was debited by exactly the notional, on the bot's own row.
    cash_updates = [
        c for c in double.store.update_docs.call_args_list
        if c[0][0] == "bots" and "$inc" in c[0][2]
    ]
    assert len(cash_updates) == 1
    assert cash_updates[0][0][1] == {"bot_id": "bot-1"}
    assert cash_updates[0][0][2]["$inc"]["cash_balance"] == pytest.approx(-10_000.0)
    assert cash_updates[0][0][2]["$inc"]["total_trades"] == 1

    # Every ledger write rode inside the ONE transaction (Tier F atomicity):
    # a fill written outside the session could survive a rolled-back position.
    for call in double.store.insert_docs.call_args_list:
        assert call.kwargs.get("session") == "session-sentinel"

    # ...and the snapshot writer fired afterward (feeding the breaker's peak).
    snap.assert_called_once_with("bot-1")
