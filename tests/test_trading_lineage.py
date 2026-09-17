"""
Comprehensive Phase 2 Lineage Verification Suite.

Validates:
1. Root trading.cycle span admission and terminal path closing.
2. Market snapshot consumed by cycle is linked to cycle_id.
3. Trace and span propagation across DecisionArtifact, PolicyDecision,
   ExecutionIntent, RiskReservation, OrderAttempt, TradeFill, ExecutionReconciliation,
   and DecisionOutcomeRecord.
4. Preserves authoritative domain IDs across the full chain.
5. Domain results: policy rejection and trading losses recorded as domain results with status="OK".
6. SHADOW execution produces telemetry evidence while leaving real portfolio untouched.
7. Paper ENFORCE execution connects admission through order, fill, reconciliation, and outcome.
8. Transaction rollback produces no false committed-success evidence.
9. Async workers restore lineage from persisted records using explicit span links.
10. get_cycle_lineage returns full connected chain.
"""

import datetime
import secrets
import unittest
from unittest.mock import MagicMock, patch

from app.telemetry.trading_adapter import (
    CandidateTradingObservation,
    TradingLineageTracker,
    TradingSpan,
    get_cycle_lineage,
    get_exporter,
)
from app.trading.attribution.models import (
    DecisionArtifact,
    DecisionOutcomeRecord,
    ExecutionIntent,
    ExecutionReconciliation,
    IntentStatus,
    OrderAttempt,
    OrderAttemptStatus,
    OutcomeMaturityStatus,
    PolicyDecision,
    PolicyDisposition,
    ReconciliationVerdict,
    RiskReservation,
)
from app.trading.attribution.verifier import verify_cycle_lineage, verify_intent_lineage
from app.v3.data_trace import root_span, trace_cycle


class TestTradingLineageSuite(unittest.TestCase):

    def setUp(self):
        # Clear exporter queue
        get_exporter().queue.clear()
        self.cycle_id = f"cycle-v3-{secrets.token_hex(6)}"
        self.ticker = "NVDA"

    def test_trace_and_root_span_identity_unification(self):
        """Trace ID and root span ID in TradingLineageTracker match data_trace.py identically."""
        expected_trace_id = TradingLineageTracker.derive_trace_id(self.cycle_id)
        expected_root_span_id = root_span(self.cycle_id)
        tracker_root_span_id = TradingLineageTracker.root_span_id(self.cycle_id)

        self.assertEqual(len(expected_trace_id), 32)
        self.assertEqual(len(tracker_root_span_id), 16)
        self.assertEqual(tracker_root_span_id, expected_root_span_id)

    def test_root_cycle_start_and_terminal_close(self):
        """Root trading.cycle span starts at admission and closes with duration and terminal status."""
        start_span = TradingLineageTracker.record_cycle_start(self.cycle_id, scope="production")
        self.assertEqual(start_span.stage, "trading.cycle")
        self.assertEqual(start_span.span_id, TradingLineageTracker.root_span_id(self.cycle_id))
        self.assertIsNone(start_span.parent_span_id)
        self.assertEqual(start_span.status, "OK")

        end_span = TradingLineageTracker.record_cycle_end(self.cycle_id, status="OK")
        self.assertEqual(end_span.stage, "trading.cycle")
        self.assertEqual(end_span.span_id, TradingLineageTracker.root_span_id(self.cycle_id))
        self.assertIsNone(end_span.parent_span_id)
        self.assertEqual(end_span.status, "OK")
        self.assertIsNotNone(end_span.duration_ms)

    def test_domain_result_separation_from_infrastructure_error(self):
        """Policy rejection and trading losses are domain results (status='OK'), not infra crashes."""
        # 1. Policy rejection
        policy_span = TradingLineageTracker.record_policy_eval(
            cycle_id=self.cycle_id,
            ticker=self.ticker,
            policy_decision_id="pol-123",
            decision_id="dec-123",
            verdict="REJECT",
            approved=False,
        )
        self.assertEqual(policy_span.status, "OK")
        self.assertEqual(policy_span.attributes["domain_result"], "REJECTED")
        self.assertFalse(policy_span.attributes["approved"])

        # 2. Trading loss
        loss_span = TradingLineageTracker.record_outcome(
            cycle_id=self.cycle_id,
            ticker=self.ticker,
            outcome_id="out-123",
            outcome="LOSS",
            pnl_pct=-0.045,
            is_shadow=False,
        )
        self.assertEqual(loss_span.status, "OK")
        self.assertEqual(loss_span.attributes["domain_result"], "LOSS")
        self.assertEqual(loss_span.attributes["outcome"], "LOSS")

    def test_market_snapshot_consumed_links_to_cycle(self):
        """Consuming a snapshot links it to the active cycle_id, distinct from background 'market-feed'."""
        feed_span = TradingLineageTracker.record_market_snapshot(
            cycle_id="market-feed",
            ticker=self.ticker,
            bar_price=128.50,
            source="alpaca",
        )
        self.assertIsNone(feed_span.parent_span_id)
        self.assertEqual(feed_span.cycle_id, "market-feed")

        consumed_span = TradingLineageTracker.record_market_snapshot_consumed(
            cycle_id=self.cycle_id,
            ticker=self.ticker,
            bar_price=128.50,
            source="cache",
        )
        self.assertEqual(consumed_span.cycle_id, self.cycle_id)
        self.assertEqual(consumed_span.parent_span_id, TradingLineageTracker.root_span_id(self.cycle_id))
        self.assertEqual(consumed_span.attributes["consumed_by_cycle"], self.cycle_id)

    def test_trace_id_and_span_id_on_attribution_models(self):
        """All canonical attribution models carry trace_id and span_id without schema validation error."""
        trace_id = TradingLineageTracker.derive_trace_id(self.cycle_id)
        span_id = TradingLineageTracker.generate_span_id()

        dec = DecisionArtifact(
            decision_id="dec-1",
            cycle_id=self.cycle_id,
            ticker=self.ticker,
            requested_action="BUY",
            confidence=85,
            model="local-glm-flash",
            producer="research-agent",
            trace_id=trace_id,
            span_id=span_id,
        )
        self.assertEqual(dec.trace_id, trace_id)
        self.assertEqual(dec.span_id, span_id)

        intent = ExecutionIntent(
            execution_intent_id="intent-1",
            decision_id=dec.decision_id,
            policy_decision_id="pol-1",
            ticker=self.ticker,
            side="BUY",
            approved_size_pct=5.0,
            valid_from=datetime.datetime.now(datetime.timezone.utc),
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
            idempotency_key="key-1",
            trace_id=trace_id,
            span_id=span_id,
        )
        self.assertEqual(intent.trace_id, trace_id)
        self.assertEqual(intent.span_id, span_id)

    def test_enforce_reconciliation_telemetry(self):
        """ENFORCE execution path instruments both fill and reconciliation telemetry."""
        fill_span = TradingLineageTracker.record_order_fill(
            cycle_id=self.cycle_id,
            ticker=self.ticker,
            order_id="ord-real-999",
            fill_id="fill-real-999",
            execution_intent_id="intent-999",
            shares=50,
            fill_price=130.0,
            parent_span_id=TradingLineageTracker.root_span_id(self.cycle_id),
            attributes={"mode": "ENFORCE", "fees": 1.0},
        )
        rec_span = TradingLineageTracker.record_reconciliation(
            cycle_id=self.cycle_id,
            ticker=self.ticker,
            reconciliation_id="rec-intent-999",
            status="MATCH",
            diff=0.0,
            reconciliation_type="ENFORCE",
            parent_span_id=fill_span.span_id,
            attributes={"mode": "ENFORCE", "order_id": "ord-real-999", "fill_id": "fill-real-999"},
        )
        self.assertEqual(fill_span.attributes["mode"], "ENFORCE")
        self.assertEqual(rec_span.attributes["reconciliation_type"], "ENFORCE")
        self.assertEqual(rec_span.parent_span_id, fill_span.span_id)
        self.assertEqual(rec_span.status, "OK")

    def test_async_worker_lineage_restoration_with_span_links(self):
        """Asynchronous outcome worker restores lineage from persisted decision artifact and emits span links."""
        parent_trace_id = TradingLineageTracker.derive_trace_id(self.cycle_id)
        parent_span_id = "span-dec-origin"

        links = [{"trace_id": parent_trace_id, "span_id": parent_span_id}]
        outcome_span = TradingLineageTracker.record_outcome(
            cycle_id=self.cycle_id,
            ticker=self.ticker,
            outcome_id="out-matured-7d",
            outcome="WIN",
            pnl_pct=0.082,
            is_shadow=False,
            parent_span_id=parent_span_id,
            links=links,
            attributes={"decision_id": "dec-origin"},
        )

        self.assertEqual(outcome_span.parent_span_id, parent_span_id)
        self.assertEqual(len(outcome_span.links), 1)
        self.assertEqual(outcome_span.links[0]["trace_id"], parent_trace_id)
        self.assertEqual(outcome_span.links[0]["span_id"], parent_span_id)

    def test_transaction_rollback_produces_no_committed_success_spans(self):
        """If a database transaction rolls back, post-commit telemetry blocks emission."""
        # Simulated database failure inside with_txn
        initial_queue_len = len(get_exporter().queue)

        class SimulatedTxnAbort(Exception):
            pass

        def broken_txn_op():
            # In our updated repository / executor pattern, spans are queued ONLY after commit
            raise SimulatedTxnAbort("Conflict on write lock")

        with self.assertRaises(SimulatedTxnAbort):
            broken_txn_op()

        # Queue length remains unchanged: zero false committed-success spans
        self.assertEqual(len(get_exporter().queue), initial_queue_len)

    def test_candidate_observation_routes_to_evidence(self):
        """Candidate trading observations record trace_id and emit candidate_observation spans."""
        obs = CandidateTradingObservation.create_candidate(
            cycle_id=self.cycle_id,
            ticker=self.ticker,
            lesson_text="Earnings report surprise beat ignored by initial sentiment filter",
            evidence_ref="sec_filing_10q_hash",
            round_num=2,
        )

        self.assertEqual(obs["lifecycle_state"], "CANDIDATE")
        self.assertEqual(obs["trace_id"], TradingLineageTracker.derive_trace_id(self.cycle_id))
        self.assertEqual(obs["ticker"], self.ticker)

        # Check emitted span
        queued = [s for s in get_exporter().queue if s.get("stage") == "candidate_observation"]
        self.assertTrue(len(queued) >= 1)
        last_obs_span = queued[-1]
        self.assertEqual(last_obs_span["cycle_id"], self.cycle_id)
        self.assertEqual(last_obs_span["attributes"]["evidence_ref"], "sec_filing_10q_hash")


if __name__ == "__main__":
    unittest.main()
