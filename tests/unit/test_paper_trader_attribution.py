"""Unit tests for paper execution attribution integration, CAS consumption,
and emergency risk exits."""
import asyncio
import datetime
from contextlib import contextmanager
from unittest.mock import MagicMock
import pytest

from app.trading import paper_trader as pt
from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    PolicyDecision,
    PolicyDisposition,
    ReconciliationVerdict,
)
from app.trading.attribution.verifier import verify_fill_lineage, verify_intent_lineage


def _run(coro):
    return asyncio.run(coro)


class _MongoDoubleBuy:
    """Mock mongo for paper_trader buy testing."""
    def __init__(self, bot_balance=10000.0, price=50.0):
        self.bot_balance = bot_balance
        self.price = price
        self.query = MagicMock()
        self.store = MagicMock()

        self.query.find_row.side_effect = self._find_row
        self.query.find_rows.side_effect = self._find_rows
        self.query.agg_row.side_effect = lambda *_a, **_k: (None,)

        from app.db.mongo_query import as_money
        self.query.as_money.side_effect = as_money

        self.store.with_txn.side_effect = self._with_txn
        self.store.dec128.side_effect = lambda v: v
        self.store.writes_mongo.side_effect = lambda _t: True
        self.store.writes_pg.side_effect = lambda _t: False
        self.store.reads_mongo.side_effect = lambda _t: True

    def _find_row(self, collection, query, columns, **kwargs):
        if collection == "bots":
            return (self.bot_balance,)
        if collection == "trade_fills":
            return None
        if collection == "price_history":
            return (self.price, None)
        if collection == "positions":
            return None
        return None

    def _find_rows(self, collection, query, columns, **kwargs):
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


def test_buy_persists_intent_and_decision_lineage(monkeypatch):
    """Verify buy() persists execution_intent_id and decision_id across records."""
    double = _MongoDoubleBuy(bot_balance=10000.0, price=50.0)
    monkeypatch.setattr(pt, "mongo_query", double.query)
    monkeypatch.setattr(pt, "mongo_store", double.store)
    monkeypatch.setattr(pt, "_classify_asset", lambda t: "stock")
    monkeypatch.setattr(pt, "_apply_execution_cost", lambda *a, **k: (50.0, {"total_bps": 0.0}))
    monkeypatch.setattr(pt, "_record_portfolio_snapshot", lambda b: None)

    # Mock intent consumption to succeed
    consumed = []
    def mock_consume(intent_id, session=None):
        consumed.append((intent_id, session))
        return True

    monkeypatch.setattr("app.trading.attribution.repository.consume_execution_intent", mock_consume)
    saved_attempts = []
    saved_recs = []
    monkeypatch.setattr("app.trading.attribution.repository.save_order_attempt", lambda att: saved_attempts.append(att))
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_reconciliation", lambda rec: saved_recs.append(rec))

    result = _run(pt.buy(
        bot_id="test-bot",
        ticker="AAPL",
        size_pct=0.10,
        current_price=50.0,
        cycle_id="cycle-1",
        execution_intent_id="intent-123",
        decision_id="dec-456",
    ))

    assert result["action"] == "BUY"
    assert result["ticker"] == "AAPL"
    assert consumed == [("intent-123", "session-sentinel")]

    # Check orders table insert
    orders = double.inserted("orders")
    assert len(orders) == 1
    assert orders[0]["execution_intent_id"] == "intent-123"
    assert orders[0]["decision_id"] == "dec-456"

    # Check trade_fills table insert
    fills = double.inserted("trade_fills")
    assert len(fills) == 1
    assert fills[0]["execution_intent_id"] == "intent-123"
    assert fills[0]["decision_id"] == "dec-456"

    # Check position_lots insert
    lots = double.inserted("position_lots")
    assert len(lots) == 1
    assert lots[0]["execution_intent_id"] == "intent-123"
    assert lots[0]["decision_id"] == "dec-456"

    # Check order attempt and reconciliation were recorded
    assert len(saved_attempts) == 1
    assert saved_attempts[0].execution_intent_id == "intent-123"
    assert len(saved_recs) == 1
    assert saved_recs[0].execution_intent_id == "intent-123"
    assert saved_recs[0].verdict == ReconciliationVerdict.EXECUTION_MATCHED


def test_buy_fails_when_intent_already_consumed(monkeypatch):
    """Verify CAS failure halts buy and inserts zero records."""
    double = _MongoDoubleBuy(bot_balance=10000.0, price=50.0)
    monkeypatch.setattr(pt, "mongo_query", double.query)
    monkeypatch.setattr(pt, "mongo_store", double.store)
    monkeypatch.setattr(pt, "_classify_asset", lambda t: "stock")

    # Intent consumption returns False (already consumed or expired)
    monkeypatch.setattr("app.trading.attribution.repository.consume_execution_intent", lambda intent_id, session=None: False)

    result = _run(pt.buy(
        bot_id="test-bot",
        ticker="AAPL",
        size_pct=0.10,
        current_price=50.0,
        cycle_id="cycle-1",
        execution_intent_id="intent-already-used",
    ))

    assert "error" in result
    assert "already consumed or expired" in result["error"]
    # Zero orders, fills, or lots inserted
    assert len(double.inserted("orders")) == 0
    assert len(double.inserted("trade_fills")) == 0
    assert len(double.inserted("position_lots")) == 0


def test_enforce_execution_intents_blocks_raw_buy_and_sell(monkeypatch):
    """When ENFORCE_EXECUTION_INTENTS is True, raw calls without intent_id are rejected."""
    monkeypatch.setattr(pt.settings, "ENFORCE_EXECUTION_INTENTS", True)
    monkeypatch.setattr(pt, "_ensure_bot", lambda b: None)

    # Raw buy
    buy_res = _run(pt.buy(bot_id="test-bot", ticker="AAPL", size_pct=0.10))
    assert "error" in buy_res
    assert "Execution intent required" in buy_res["error"]

    # Raw sell
    sell_res = _run(pt.sell(bot_id="test-bot", ticker="AAPL", qty_pct=1.0))
    assert "error" in sell_res
    assert "Execution intent required" in sell_res["error"]


def test_emergency_risk_exit_exempt_from_intent_enforcement(monkeypatch):
    """emergency_risk_exit bypasses ENFORCE_EXECUTION_INTENTS."""
    monkeypatch.setattr(pt.settings, "ENFORCE_EXECUTION_INTENTS", True)
    monkeypatch.setattr(pt, "_ensure_bot", lambda b: None)

    # Mock sell to succeed
    sell_calls = []
    async def mock_sell(**kwargs):
        sell_calls.append(kwargs)
        return {"status": "success", "closed_lots": 1}

    monkeypatch.setattr(pt, "sell", mock_sell)

    res = _run(pt.emergency_risk_exit(bot_id="test-bot", ticker="AAPL", reason="CIRCUIT_BREAKER", qty_pct=1.0))
    assert res["status"] == "success"
    assert len(sell_calls) == 1
    assert sell_calls[0]["is_emergency_risk_exit"] is True
    assert sell_calls[0].get("execution_intent_id") is None


def test_verify_fill_lineage_contract(monkeypatch):
    """Verify verify_fill_lineage and verify_intent_lineage validate lineage unbroken chains."""
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="intent-1",
        decision_id="dec-1",
        policy_decision_id="pol-1",
        ticker="AAPL",
        side="BUY",
        approved_quantity=10.0,
        approved_notional=1000.0,
        approved_size_pct=0.10,
        reference_quote={"price": 100.0},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=2),
        idempotency_key="idem-1",
    )
    policy = PolicyDecision(
        policy_decision_id="pol-1",
        decision_id="dec-1",
        policy_version="1.0.0",
        config_hash="abc",
        evaluation_timestamp=now,
        disposition=PolicyDisposition.APPROVE,
        normalized_ticker="AAPL",
        normalized_action="BUY",
        approved_size_pct=0.10,
        requested_values={"action": "BUY", "size_pct": 0.10},
        normalized_values={"action": "BUY", "size_pct": 0.10},
        approved_values={"action": "BUY", "size_pct": 0.10},
    )
    artifact = DecisionArtifact(
        decision_id="dec-1",
        cycle_id="cycle-1",
        ticker="AAPL",
        asset_type="stock",
        producer="v3_decision_synthesizer",
        model="local-test-model",
        requested_action="BUY",
        confidence=80,
        reference_quote={"price": 100.0},
    )

    # Mock getters in verifier
    monkeypatch.setattr("app.trading.attribution.verifier.get_execution_intent", lambda i_id: intent if i_id == "intent-1" else None)
    monkeypatch.setattr("app.trading.attribution.verifier.get_policy_decision", lambda p_id: policy if p_id == "pol-1" else None)
    monkeypatch.setattr("app.trading.attribution.verifier.get_decision_artifact", lambda d_id: artifact if d_id == "dec-1" else None)
    monkeypatch.setattr("app.trading.attribution.verifier.mongo_store.find_docs", lambda coll, q, **kw: [])

    # Complete lineage test
    res_intent = verify_intent_lineage("intent-1")
    assert res_intent.valid is True
    assert len(res_intent.anomalies) == 0
    assert "policy" in res_intent.chain
    assert "artifact" in res_intent.chain

    # Mock fill
    monkeypatch.setattr("app.trading.attribution.verifier.mongo_store.find_docs", lambda coll, q, **kw: [{"fill_id": "fill-1", "execution_intent_id": "intent-1"}] if coll == "trade_fills" and q.get("fill_id") == "fill-1" else [])

    res_fill = verify_fill_lineage("fill-1")
    assert res_fill.valid is True
    assert len(res_fill.anomalies) == 0

    # Test orphan fill
    monkeypatch.setattr("app.trading.attribution.verifier.mongo_store.find_docs", lambda coll, q, **kw: [{"fill_id": "fill-orphan"}] if coll == "trade_fills" and q.get("fill_id") == "fill-orphan" else [])
    res_orphan = verify_fill_lineage("fill-orphan")
    assert res_orphan.valid is False
    assert any("unattributed fill" in a for a in res_orphan.anomalies)
