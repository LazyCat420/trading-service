"""Unit tests for Phase 6: Snapshot Hardening, Environmental Degradation, and Operational Metrics.

Verifies:
1. Quote age None defaults to 999.0 and triggers is_degraded = True.
2. Order capacity lookup exception gracefully sets is_degraded = True without crashing snapshot.
3. Degraded environment causes PolicyTranslator to REJECT risk-increasing BUY orders.
4. Degraded environment allows risk-reducing SELL orders to execute protective exits.
5. Control plane operational telemetry aggregates deployed commit SHA, worker heartbeats (ALIVE/DEAD),
   reconciliation latency, outbox retries, and outcome coverage.
"""

import datetime
import pytest
from unittest.mock import patch, MagicMock

from app.trading.attribution.models import DecisionArtifact, PolicyDisposition
from app.trading.policy.policy_translator import PolicyInputSnapshot, PolicyTranslator
from app.trading.policy.snapshot_service import build_policy_snapshot
from app.trading.control_plane import get_control_plane_operational_metrics


class DictCollection:
    def __init__(self, data=None):
        self.data = list(data) if data else []

    def find(self, query=None, projection=None, session=None):
        query = query or {}
        matched = []
        for doc in self.data:
            match = True
            for k, v in query.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                matched.append(dict(doc))
        class Cursor:
            def __init__(self, items):
                self.items = items
            def sort(self, *args, **kwargs):
                return self
            def limit(self, n):
                if n > 0:
                    self.items = self.items[:n]
                return self
            def __iter__(self):
                return iter(self.items)
            def __getitem__(self, idx):
                return self.items[idx]
        return Cursor(matched)

    def find_one(self, query=None, sort=None, session=None):
        items = self.find(query=query, session=session).items
        return dict(items[0]) if items else None

    def insert_one(self, doc, session=None):
        self.data.append(dict(doc))
        res = MagicMock()
        res.inserted_id = doc.get("_id", "mock-id")
        return res

    def count_documents(self, query=None):
        return len(self.find(query=query).items)

    def update_one(self, filter_q, update_q, upsert=False, session=None):
        res = MagicMock()
        res.matched_count = 1
        res.modified_count = 1
        return res


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, item):
        if item not in self.cols:
            self.cols[item] = DictCollection()
        return self.cols[item]


def test_snapshot_quote_age_none_defaults_to_999_and_degraded():
    fake_db = FakeDB()
    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_query.find_row", return_value=[10000.0, 10000.0]), \
         patch("app.db.mongo_query.find_rows", return_value=[]), \
         patch("app.trading.order_capacity.pending_capacity", return_value={"cash_reserved": 0.0, "ticker_reserved": 0.0}), \
         patch("app.db.mongo_store.insert_docs", return_value=None):

        snapshot, payload = build_policy_snapshot(
            bot_id="bot-test",
            ticker="AAPL",
            quote_price=150.0,
            quote_age_hours=None,  # Intentionally None
        )

        assert snapshot.quote_age_hours == 999.0
        assert snapshot.is_degraded is True
        assert any("TARGET_QUOTE_STALE_AAPL" in r for r in snapshot.degraded_reasons)
        assert payload["data_quality"]["stale_quote"] is True


def test_snapshot_capacity_lookup_error_sets_degraded():
    fake_db = FakeDB()
    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_query.find_row", return_value=[10000.0, 10000.0]), \
         patch("app.db.mongo_query.find_rows", return_value=[]), \
         patch("app.trading.order_capacity.pending_capacity", side_effect=RuntimeError("Mongo cluster failure")), \
         patch("app.db.mongo_store.insert_docs", return_value=None):

        snapshot, payload = build_policy_snapshot(
            bot_id="bot-test",
            ticker="MSFT",
            quote_price=300.0,
            quote_age_hours=0.5,  # Fresh quote
        )

        assert snapshot.quote_age_hours == 0.5
        assert snapshot.is_degraded is True
        assert any("CAPACITY_LOOKUP_FAILED" in r for r in snapshot.degraded_reasons)


def test_degraded_snapshot_rejects_buy_in_policy_translator():
    now = datetime.datetime.now(datetime.timezone.utc)
    artifact = DecisionArtifact(
        decision_id="dec-deg-buy-1",
        cycle_id="cycle-1",
        ticker="GOOGL",
        requested_action="BUY",
        reference_quote={"price": 100.0},
        producer="test-agent",
        model="test-model",
        confidence=85,
        created_at=now,
    )

    degraded_snapshot = PolicyInputSnapshot(
        portfolio_equity=10000.0,
        cash_balance=10000.0,
        bot_id="bot-test",
        quote_price=100.0,
        quote_age_hours=0.5,
        is_degraded=True,
        degraded_reasons=["CAPACITY_LOOKUP_FAILED_MOCK"],
    )

    pol_dec, intent = PolicyTranslator.evaluate(artifact, degraded_snapshot)

    assert pol_dec.disposition == PolicyDisposition.REJECT
    assert "DEGRADED_ENVIRONMENT" in pol_dec.reason_codes
    assert pol_dec.gate_results["data_freshness_and_capacity"]["status"] == "FAIL"
    assert intent is None


def test_degraded_snapshot_allows_sell_exit():
    now = datetime.datetime.now(datetime.timezone.utc)
    artifact = DecisionArtifact(
        decision_id="dec-deg-sell-1",
        cycle_id="cycle-1",
        ticker="GOOGL",
        requested_action="SELL",
        reference_quote={"price": 100.0},
        producer="test-agent",
        model="test-model",
        confidence=85,
        created_at=now,
    )

    # Bot holds 10 shares of GOOGL
    degraded_snapshot = PolicyInputSnapshot(
        portfolio_equity=10000.0,
        cash_balance=9000.0,
        bot_id="bot-test",
        quote_price=100.0,
        quote_age_hours=0.5,
        held_positions={"GOOGL": 10.0},
        held_ticker_value=1000.0,
        is_held=True,
        is_degraded=True,
        degraded_reasons=["CAPACITY_LOOKUP_FAILED_MOCK"],
    )

    pol_dec, intent = PolicyTranslator.evaluate(artifact, degraded_snapshot)

    # Protective exits must not be blocked by degraded environments!
    assert pol_dec.disposition == PolicyDisposition.APPROVE
    assert "DEGRADED_ENVIRONMENT" not in pol_dec.reason_codes
    assert intent is not None
    assert intent.side == "SELL"


def test_control_plane_operational_metrics():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)

    # Insert worker heartbeats
    fake_db["worker_heartbeats"].insert_one({
        "worker": "outbox_worker",
        "last_heartbeat": now - datetime.timedelta(seconds=10),
        "status": "RUNNING",
    })
    fake_db["worker_heartbeats"].insert_one({
        "worker": "stale_worker",
        "last_heartbeat": now - datetime.timedelta(seconds=300),
        "status": "RUNNING",
    })

    # Insert reconciliation
    fake_db["execution_reconciliations"].insert_one({
        "reconciliation_id": "rec-test-1",
        "verdict": "EXECUTION_MATCHED",
        "reconciled_at": now - datetime.timedelta(minutes=2),
    })

    # Insert decisions and outcomes
    fake_db["decision_artifacts"].insert_one({"decision_id": "dec-1"})
    fake_db["decision_artifacts"].insert_one({"decision_id": "dec-2"})
    fake_db["decision_outcomes"].insert_one({
        "outcome_id": "out-1",
        "maturity_status": "MATURE",
        "resolved_at": now - datetime.timedelta(minutes=5),
    })

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.trading.outbox.repository.get_outbox_metrics", return_value={"pending": 0, "failed": 0, "retry_totals": 0}), \
         patch("app.trading.control_plane.resolve_control_plane_mode", return_value=MagicMock(value="ENFORCE")):

        metrics = get_control_plane_operational_metrics()

        assert "deployed_commit_sha" in metrics
        assert metrics["default_mode"] == "ENFORCE"

        # Worker heartbeats
        hb = metrics["worker_heartbeats"]
        assert hb["outbox_worker"]["status"] == "ALIVE"
        assert hb["stale_worker"]["status"] == "DEAD"

        # Outbox metrics
        assert "outbox" in metrics

        # Reconciliation telemetry
        assert metrics["reconciliation"]["last_reconciliation_id"] == "rec-test-1"
        assert metrics["reconciliation"]["last_verdict"] == "EXECUTION_MATCHED"

        # Outcome coverage
        assert metrics["outcome_evaluation"]["total_decisions"] == 2
        assert metrics["outcome_evaluation"]["mature_decisions"] == 1
        assert metrics["outcome_evaluation"]["coverage_pct"] == 50.0
