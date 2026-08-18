"""Fund alerts: direct creation, and the stop-loss path that raises one.

This used to seed a real Postgres test database (`real_db`), patch
`alert_service.get_db` / `paper_trader.get_db`, and read `fund_alerts` back
with SQL. Neither module imports `get_db` any more — both write through
`mongo_store` — so the patches raised and the whole test was skipped whenever
TRADING_BOT_TEST_DB was unset.

Rewritten against the Mongo layer. Both halves survive: the direct call, and
the end-to-end stop-loss breach that must fire a SELL *and* record a high
severity alert naming the ticker. The alert is now read out of the document
handed to `insert_docs`, which pins the collection and every field, where the
SQL round-trip only pinned the three columns it happened to SELECT.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.services.alert_service import record_fund_alert
from app.trading.paper_trader import check_stop_losses


def _alerts(store):
    """Every document written to fund_alerts through this store double."""
    out = []
    for call in store.insert_docs.call_args_list:
        if call[0][0] == "fund_alerts":
            out.extend(call[0][1])
    return out


def test_fund_alert_direct_creation():
    """record_fund_alert validates, writes one fund_alerts document, and
    returns the validated alert."""
    store = MagicMock()
    with patch("app.services.alert_service.mongo_store", store):
        result = record_fund_alert(
            alert_type="anomaly",
            entity_name="test-bot",
            detail="Testing alert service directly",
            severity="low",
        )

    assert "error" not in result
    assert result["alert_type"] == "anomaly"

    alerts = _alerts(store)
    assert len(alerts) == 1
    a = alerts[0]
    assert (a["alert_type"], a["entity_name"], a["severity"]) == ("anomaly", "test-bot", "low")
    assert a["detail"] == "Testing alert service directly"
    assert a["id"] == result["id"]
    assert a["is_read"] is False


class _MongoDouble:
    """Collection-keyed stand-in for paper_trader's mongo_query + mongo_store."""

    def __init__(self, positions, price):
        self._positions = positions
        self._price = price
        self.query = MagicMock()
        self.store = MagicMock()

        self.query.find_row.side_effect = self._find_row
        self.query.find_rows.side_effect = self._find_rows
        self.query.agg_row.side_effect = lambda *_a, **_k: (None,)

        self.store.with_txn.side_effect = self._with_txn
        # Value-preserving, so a monetary assertion reads the stored number.
        self.store.dec128.side_effect = lambda v: v
        # Branch predicates — a bare MagicMock is truthy for both, which
        # silently exercises the dual-write path.
        self.store.writes_mongo.side_effect = lambda _t: True
        self.store.writes_pg.side_effect = lambda _t: False
        self.store.reads_mongo.side_effect = lambda _t: True
        self.store.find_docs.side_effect = lambda *_a, **_k: []

    def _find_row(self, collection, query, columns, **kwargs):
        if collection == "trade_fills":
            return None
        if collection == "positions":
            # (id, qty, avg_entry_price) for the SELL that the stop triggers
            return ("pos-123", 10.0, 150.0)
        if collection == "price_history":
            return (self._price, None)
        if collection == "bots":
            return ("test-bot",)
        return None

    def _find_rows(self, collection, query, columns, **kwargs):
        if collection == "positions":
            return list(self._positions)
        if collection == "position_lots":
            return [("lot-abc", 10.0, 150.0, None)]
        return []

    @contextmanager
    def _with_txn(self):
        yield "session-sentinel"


@pytest.mark.asyncio
async def test_stop_loss_breach_records_a_high_severity_alert():
    """A breached stop must sell AND raise a 'stop_loss' alert for the ticker.

    Entry 150 with an 8% stop puts the line at $138; the mark is $130, so the
    stop is breached.
    """
    double = _MongoDouble(
        # (id, ticker, qty, avg_entry_price, stop_loss_pct, exit_style)
        positions=[("pos-123", "AAPL", 10.0, 150.0, 0.08, "hard_stop")],
        price=130.0,
    )
    alert_store = MagicMock()

    patches = [
        patch("app.trading.paper_trader.mongo_query", double.query),
        patch("app.trading.paper_trader.mongo_store", double.store),
        patch("app.trading.paper_trader._ensure_bot", lambda *_a, **_k: None),
        patch("app.trading.paper_trader._record_portfolio_snapshot", MagicMock()),
        patch("app.services.alert_service.mongo_store", alert_store),
    ]
    for p in patches:
        p.start()
    try:
        triggered = await check_stop_losses("test-bot", cycle_id="test-cycle")
    finally:
        for p in patches:
            p.stop()

    assert len(triggered) == 1
    assert triggered[0]["action"] == "SELL"
    assert triggered[0]["ticker"] == "AAPL"
    assert triggered[0]["qty"] == 10.0

    alerts = _alerts(alert_store)
    assert len(alerts) == 1
    a = alerts[0]
    assert (a["ticker"], a["entity_name"], a["severity"]) == ("AAPL", "test-bot", "high")
    assert a["alert_type"] == "stop_loss"
    # The detail must carry the numbers a reader needs to audit the exit.
    assert "AAPL" in a["detail"]
    assert "130.00" in a["detail"]
    assert "150.00" in a["detail"]


@pytest.mark.asyncio
async def test_stop_not_breached_raises_no_alert():
    """A position ABOVE its stop must neither sell nor alert.

    The original test only exercised the firing side, so a check_stop_losses
    that fired unconditionally would have passed it.
    """
    double = _MongoDouble(
        positions=[("pos-123", "AAPL", 10.0, 150.0, 0.08, "hard_stop")],
        price=145.0,   # stop line is 138.00 — not breached
    )
    alert_store = MagicMock()

    patches = [
        patch("app.trading.paper_trader.mongo_query", double.query),
        patch("app.trading.paper_trader.mongo_store", double.store),
        patch("app.trading.paper_trader._ensure_bot", lambda *_a, **_k: None),
        patch("app.trading.paper_trader._record_portfolio_snapshot", MagicMock()),
        patch("app.services.alert_service.mongo_store", alert_store),
    ]
    for p in patches:
        p.start()
    try:
        triggered = await check_stop_losses("test-bot", cycle_id="test-cycle")
    finally:
        for p in patches:
            p.stop()

    assert triggered == []
    assert _alerts(alert_store) == []
    double.store.insert_docs.assert_not_called()
