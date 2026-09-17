"""Unit tests for Phase 5: Replayable Reconciliation, Outbox Lease Tokens, and Horizon Outcome Attribution.

Verifies:
1. Outbox lease owner token prevents expired or foreign workers from updating reclaimed events.
2. Reconcile execution produces stable deterministic reconciliation_id: f"rec-{intent_id}".
3. Outcome worker queries historical prices at declared maturity timestamp, not execution time.
4. Outcome evaluation progresses across pagination batches without starvation.
5. Missing benchmark produces UNRESOLVED, which resolves to MATURE after backfill.
6. Closed lot closures evaluate alpha via LotAlphaEvaluator.
"""

import datetime
import pytest
from unittest.mock import patch, MagicMock

from app.trading.attribution.models import (
    DecisionArtifact,
    DecisionOutcomeRecord,
    ExecutionIntent,
    OutcomeMaturityStatus,
    OrderAttempt,
)
from app.trading.attribution.reconciliation import reconcile_execution
from app.trading.attribution.worker import (
    evaluate_decision_at_horizon,
    run_mature_outcome_evaluation_iteration,
    evaluate_closed_lot_alpha_iteration,
    COLL_DECISION_OUTCOMES,
)
from app.trading.outbox.repository import (
    COLL_EXECUTION_OUTBOX,
    claim_pending_outbox_events,
    mark_outbox_event_completed,
    mark_outbox_event_failed,
)


def match_doc(doc, query):
    if not query:
        return True
    for k, v in query.items():
        if k == "$or":
            if not any(match_doc(doc, branch) for branch in v):
                return False
        elif k == "$and":
            if not all(match_doc(doc, branch) for branch in v):
                return False
        else:
            val = doc.get(k)
            if isinstance(v, dict):
                for op, op_val in v.items():
                    if op == "$gte" and (val is None or val < op_val):
                        return False
                    elif op == "$gt" and (val is None or val <= op_val):
                        return False
                    elif op == "$lte" and (val is None or val > op_val):
                        return False
                    elif op == "$lt" and (val is None or val >= op_val):
                        return False
                    elif op == "$ne" and val == op_val:
                        return False
                    elif op == "$in" and val not in op_val:
                        return False
                    elif op == "$nin" and val in op_val:
                        return False
            elif val != v:
                return False
    return True


class DictCollection:
    def __init__(self, data=None):
        self.data = list(data) if data else []

    def find(self, query=None, projection=None, session=None):
        query = query or {}
        matched = [dict(doc) for doc in self.data if match_doc(doc, query)]
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
        cursor = self.find(query=query, session=session)
        items = cursor.items
        return dict(items[0]) if items else None

    def find_one_and_update(self, filter_q, update_q, session=None, return_document=False):
        for doc in self.data:
            if match_doc(doc, filter_q):
                if "$set" in update_q:
                    doc.update(update_q["$set"])
                return dict(doc)
        return None

    def insert_one(self, doc, session=None):
        self.data.append(dict(doc))
        res = MagicMock()
        res.inserted_id = doc.get("_id", "mock-id")
        return res

    def insert_many(self, docs, ordered=True, session=None, *args, **kwargs):
        for doc in docs:
            self.insert_one(doc, session=session)
        res = MagicMock()
        res.inserted_ids = [doc.get("_id", "mock-id") for doc in docs]
        return res

    def update_one(self, filter_q, update_q, upsert=False, session=None):
        matched = 0
        modified = 0
        for doc in self.data:
            if match_doc(doc, filter_q):
                matched += 1
                if "$set" in update_q:
                    doc.update(update_q["$set"])
                modified += 1
                break
        if matched == 0 and upsert:
            new_doc = dict(filter_q)
            if "$set" in update_q:
                new_doc.update(update_q["$set"])
            self.data.append(new_doc)
            matched = 1
            modified = 1
        res = MagicMock()
        res.matched_count = matched
        res.modified_count = modified
        return res

    def aggregate(self, pipeline):
        return []


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, item):
        if item not in self.cols:
            self.cols[item] = DictCollection()
        return self.cols[item]


def test_outbox_lease_owner_token_enforcement():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)
    event_id = "evt-outbox-1"
    fake_db[COLL_EXECUTION_OUTBOX].insert_one({
        "event_id": event_id,
        "status": "PENDING",
        "retry_after": None,
        "created_at": now,
    })

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db):
        # Worker 1 claims event
        claimed = claim_pending_outbox_events(batch_size=1, worker_token="worker-token-A")
        assert len(claimed) == 1
        assert claimed[0]["locked_by"] == "worker-token-A"

        # Worker 2 with different token tries to complete it -> MUST FAIL (returns False)
        ok_wrong = mark_outbox_event_completed(event_id, result_payload={"ok": True}, worker_token="worker-token-B")
        assert ok_wrong is False

        # Event status should still be PROCESSING
        doc = fake_db[COLL_EXECUTION_OUTBOX].find_one({"event_id": event_id})
        assert doc["status"] == "PROCESSING"

        # Worker 1 with correct token completes it -> SUCCESS
        ok_correct = mark_outbox_event_completed(event_id, result_payload={"ok": True}, worker_token="worker-token-A")
        assert ok_correct is True
        doc_after = fake_db[COLL_EXECUTION_OUTBOX].find_one({"event_id": event_id})
        assert doc_after["status"] == "COMPLETED"


def test_deterministic_reconciliation_identity():
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="intent-det-rec-99",
        decision_id="dec-99",
        policy_decision_id="pol-99",
        ticker="AAPL",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.01,
        valid_from=now - datetime.timedelta(minutes=1),
        expires_at=now + datetime.timedelta(minutes=10),
        idempotency_key="key-det-99",
        bot_id="bot-test",
    )
    rec1 = reconcile_execution(
        intent=intent,
        attempts=[],
        order={"price": 100.0},
        fills=[{"qty": 10.0, "price": 100.0, "fees": 1.0, "filled_at": now}],
    )
    rec2 = reconcile_execution(
        intent=intent,
        attempts=[],
        order={"price": 100.0},
        fills=[{"qty": 10.0, "price": 100.0, "fees": 1.0, "filled_at": now}],
    )
    # Both reconciliations must have the exact same deterministic ID
    assert rec1.reconciliation_id == "rec-intent-det-rec-99"
    assert rec2.reconciliation_id == "rec-intent-det-rec-99"
    assert rec1.reconciliation_id == rec2.reconciliation_id


def test_outcome_historical_horizon_price_query_and_backfill():
    fake_db = FakeDB()
    entry_time = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.timezone.utc)
    horizon_days = 7
    maturity_date = entry_time + datetime.timedelta(days=horizon_days)  # 2026-08-08
    eval_time = datetime.datetime(2026, 9, 1, 10, 0, tzinfo=datetime.timezone.utc)

    artifact = DecisionArtifact(
        decision_id="dec-hist-1",
        cycle_id="cycle-1",
        ticker="MSFT",
        requested_action="BUY",
        reference_quote={"price": 200.0},
        producer="test-agent",
        model="test-model",
        confidence=85,
        created_at=entry_time,
        declared_horizon_days=horizon_days,
        benchmark_symbol="SPY",
    )

    # First test: Benchmark data is missing at maturity -> UNRESOLVED
    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.trading.attribution.worker._get_asset_historical_price", return_value=220.0), \
         patch("app.trading.attribution.worker._get_benchmark_price", return_value=None):

        out_unresolved = evaluate_decision_at_horizon(artifact, now=eval_time)
        assert out_unresolved is not None
        assert out_unresolved.maturity_status == OutcomeMaturityStatus.UNRESOLVED

    # Second test: Benchmark data has been backfilled -> resolves to MATURE
    def mock_benchmark_price(sym, target_dt):
        if target_dt == entry_time:
            return 400.0
        return 420.0  # at maturity_date

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.trading.attribution.worker._get_asset_historical_price") as mock_asset_price, \
         patch("app.trading.attribution.worker._get_benchmark_price", side_effect=mock_benchmark_price):

        mock_asset_price.return_value = 220.0

        out_mature = evaluate_decision_at_horizon(artifact, now=eval_time)
        assert out_mature is not None
        assert out_mature.maturity_status == OutcomeMaturityStatus.MATURE
        # Ensure asset price was queried at maturity_date (2026-08-08), NOT eval_time (2026-09-01)!
        mock_asset_price.assert_called_with("MSFT", maturity_date)
        # Decision return: (220-200)/200 = +10.0%, Benchmark return: (420-400)/400 = +5.0% -> Alpha = +5.0%
        assert out_mature.decision_alpha == pytest.approx(5.0, 0.01)


def test_outcome_pagination_no_starvation():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)
    old_time = now - datetime.timedelta(days=30)

    # Insert 60 mature artifacts
    artifacts = []
    for i in range(60):
        art = {
            "decision_id": f"dec-starve-{i}",
            "cycle_id": f"cycle-{i}",
            "ticker": "AAPL",
            "requested_action": "BUY",
            "reference_quote": {"price": 100.0},
            "producer": "test-agent",
            "model": "test-model",
            "confidence": 85,
            "created_at": old_time + datetime.timedelta(minutes=i),
            "declared_horizon_days": 7,
            "benchmark_symbol": "SPY",
            "outcome_status": "PENDING",
            "retry_after": None,
        }
        fake_db["decision_artifacts"].insert_one(art)

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.trading.attribution.worker._get_asset_historical_price", return_value=110.0), \
         patch("app.trading.attribution.worker._get_benchmark_price", return_value=400.0):

        # Batch 1: evaluates 50
        batch1_count = run_mature_outcome_evaluation_iteration(limit=50)
        assert batch1_count == 50

        # Batch 2: evaluates the remaining 10 without starving on the first 50!
        batch2_count = run_mature_outcome_evaluation_iteration(limit=50)
        assert batch2_count == 10


def test_closed_lot_alpha_evaluation():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)

    # Insert a closed lot record
    closure_doc = {
        "closure_id": "close-test-1",
        "lot_id": "lot-test-1",
        "bot_id": "bot-test",
        "ticker": "AAPL",
        "closed_qty": 10.0,
        "entry_price": 100.0,
        "exit_price": 120.0,
        "fees": 2.0,
        "closed_at": now,
        "alpha_evaluated": False,
    }
    fake_db["lot_closures"].insert_one(closure_doc)
    fake_db["position_lots"].insert_one({
        "lot_id": "lot-test-1",
        "opened_at": now - datetime.timedelta(days=10),
        "origin": "HISTORICAL_RECONSTRUCTION",
        "provenance_complete": True,
    })

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.trading.attribution.worker._get_benchmark_price", side_effect=[400.0, 420.0]):

        evaluated_count = evaluate_closed_lot_alpha_iteration(limit=10)
        assert evaluated_count == 1

        # Verify closure document is marked evaluated
        updated_closure = fake_db["lot_closures"].find_one({"closure_id": "close-test-1"})
        assert updated_closure["alpha_evaluated"] is True
        assert "lot_alpha" in updated_closure

        # Verify evaluation record in lot_closure_evaluations
        eval_doc = fake_db["lot_closure_evaluations"].find_one({"closure_id": "close-test-1"})
        assert eval_doc is not None
        assert eval_doc["is_attributable"] is True
        assert eval_doc["provenance"] == "HISTORICAL_RECONSTRUCTION"
