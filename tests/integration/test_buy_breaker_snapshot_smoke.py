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
    (no trade_fills row written), with the REAL breaker running; and
  * a healthy book lets the BUY through and fires the snapshot writer afterward.

Uses an order-independent DB mock keyed on SQL text (not call order), so it
survives refactors of the read sequence inside buy(). Marked `integration`
because it exercises the whole execution path, not one function.
"""
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.trading import paper_trader

pytestmark = pytest.mark.integration


class _SmartCursor:
    """A cursor whose reads are keyed on SQL substrings, not call order.

    buy() issues several ordered reads (dup-check, cash, price sanity, positions,
    then the breaker's peak read). Keying on the SQL keeps the test robust if
    that order changes.
    """

    def __init__(self, cash, peak, positions_rows):
        self.cash = cash
        self.peak = peak
        self.positions_rows = positions_rows
        self._last_sql = ""
        self.executed = []  # (sql, params) for every execute()

    def execute(self, sql, params=None):
        self._last_sql = sql
        self.executed.append((sql, params))
        return self

    def fetchone(self):
        s = self._last_sql
        if "cash_balance FROM bots" in s:
            return (self.cash,)
        if "FROM price_history" in s:
            return (None,)  # no history → skip the price-sanity gate
        if "MAX(total_value) FROM portfolio_snapshots" in s:
            return (self.peak,) if self.peak is not None else None
        if "FROM trade_fills" in s:
            return None  # no duplicate BUY in cycle
        if "SELECT id, qty, avg_entry_price FROM positions" in s:
            return None  # no existing position → INSERT branch
        return None

    def fetchall(self):
        if "FROM positions" in self._last_sql:
            return self.positions_rows
        return []

    # ── context-manager + transaction plumbing ──
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        cur = self

        @contextmanager
        def _txn():
            yield cur

        return _txn()

    def executed_sql(self):
        return [sql for sql, _ in self.executed]


def _patch_buy(cursor):
    """Patch get_db + the external helpers buy() reaches, leaving the real
    breaker and the real execution/gating logic intact."""
    @contextmanager
    def _get_db():
        yield cursor

    return [
        patch("app.trading.paper_trader.get_db", _get_db),
        patch("app.trading.paper_trader._ensure_bot", lambda *_a, **_k: None),
        patch("app.trading.paper_trader._compute_stop_loss_pct", return_value=0.08),
    ]


@pytest.mark.asyncio
async def test_breaker_blocks_buy_on_the_live_path():
    # Book is 50k against a 100k peak → -50% drawdown, past the 25% limit.
    cursor = _SmartCursor(cash=50_000.0, peak=100_000.0, positions_rows=[])
    patches = _patch_buy(cursor)
    for p in patches:
        p.start()
    try:
        with patch.object(paper_trader.settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.25):
            result = await paper_trader.buy("bot-1", "AAPL", size_pct=0.10, current_price=150.0)
    finally:
        for p in patches:
            p.stop()

    # Real breaker fired → BUY refused with the breaker's error, and nothing
    # was written to the broker ledger.
    assert "error" in result
    assert "drawdown breaker" in result["error"].lower()
    assert not any("INSERT INTO trade_fills" in s for s in cursor.executed_sql()), \
        "a blocked BUY must not write a fill"


@pytest.mark.asyncio
async def test_healthy_book_executes_and_snapshots():
    # Book is at its 100k peak → 0% drawdown → breaker allows the BUY.
    cursor = _SmartCursor(cash=100_000.0, peak=100_000.0, positions_rows=[])
    patches = _patch_buy(cursor)
    for p in patches:
        p.start()
    snap = MagicMock()
    snap_patch = patch("app.trading.paper_trader._record_portfolio_snapshot", snap)
    snap_patch.start()
    try:
        with patch.object(paper_trader.settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.25):
            result = await paper_trader.buy("bot-1", "AAPL", size_pct=0.10, current_price=150.0)
    finally:
        snap_patch.stop()
        for p in patches:
            p.stop()

    # BUY went through the real path...
    assert result.get("action") == "BUY", f"expected a BUY, got {result}"
    assert any("INSERT INTO trade_fills" in s for s in cursor.executed_sql()), \
        "a cleared BUY must write a fill"
    # ...and the snapshot writer fired afterward (feeding the breaker's peak).
    snap.assert_called_once_with("bot-1")
