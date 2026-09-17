"""
Trading Telemetry Adapter & Lineage Tracker.

Traces the complete execution lineage for trading cycles:
cycle → market snapshot → research/model call → decision → policy →
execution intent → reservation/slot → order/fill → reconciliation → outcome.

Provides:
- Deterministic trace_id and span_id generation
- Propagates trace_id across decision, policy, intent, order, and outcome records
- Safe asynchronous exporter to NAS telemetry collector
- Candidate observation recording for trading learning
"""

import collections
import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_COLLECTOR_URL = os.environ.get(
    "TELEMETRY_COLLECTOR_URL", "http://10.0.0.16:5595/v1/telemetry"
)


def _sanitize_dict(obj: Any) -> Any:
    """Sanitizes sensitive keys to prevent credential leakage."""
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            if re.search(r"password|secret|token|api[_-]?key|credential|private[_-]?key", k, re.IGNORECASE):
                clean[k] = "[REDACTED]"
            else:
                clean[k] = _sanitize_dict(v)
        return clean
    if isinstance(obj, list):
        return [_sanitize_dict(item) for item in obj]
    if isinstance(obj, str):
        return re.sub(
            r"<(think|thought_process|analysis|reasoning)\b[^>]*>[\s\S]*?(?:<\/\1>|$)",
            "[private reasoning omitted]",
            obj,
            flags=re.IGNORECASE,
        )
    return obj


@dataclass
class TradingSpan:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    cycle_id: str
    ticker: str
    stage: str  # e.g., "trading.cycle", "market_snapshot", "research", "decision", "policy", "execution_intent", "reservation_slot", "order_fill", "reconciliation", "outcome"
    status: str  # "OK" | "ERROR" | "CANCELLED"
    start_time: str
    end_time: Optional[str] = None
    duration_ms: Optional[int] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    links: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TelemetryAsyncExporter:
    """Non-blocking background thread exporter for telemetry spans to NAS."""

    def __init__(self, endpoint: str = DEFAULT_COLLECTOR_URL, max_queue: int = 2000):
        self.endpoint = endpoint
        self.max_queue = max_queue
        self.queue: collections.deque = collections.deque(maxlen=max_queue)
        self._running = True
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    def enqueue(self, span: TradingSpan) -> None:
        clean_span = _sanitize_dict(span.to_dict())
        with self._lock:
            self.queue.append(clean_span)

    def flush(self) -> None:
        batch = []
        with self._lock:
            while self.queue and len(batch) < 50:
                batch.append(self.queue.popleft())

        if not batch:
            return

        payload = {
            "schema_version": "1.0",
            "service_source": "trading-service",
            "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "spans": batch,
            "runs": [],
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # Fail fast with 2.5s timeout — never block trading loops
            with urllib.request.urlopen(req, timeout=2.5):
                pass
        except Exception as e:
            # Silently catch; telemetry failures never compromise trading operations
            logger.debug("[TelemetryAsyncExporter] Export failed: %s", e)

    def _worker_loop(self) -> None:
        while self._running:
            try:
                time.sleep(1.0)
                self.flush()
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False


# Global exporter instance
_exporter: Optional[TelemetryAsyncExporter] = None


def get_exporter() -> TelemetryAsyncExporter:
    global _exporter
    if _exporter is None:
        _exporter = TelemetryAsyncExporter()
    return _exporter


class TradingLineageTracker:
    """Tracks and emits lineage spans across the complete trading lifecycle."""

    _cycle_start_times: dict[str, float] = {}

    @staticmethod
    def derive_trace_id(cycle_id: str) -> str:
        """Deterministically derives a 32-char hex trace_id from cycle_id."""
        return hashlib.sha256(cycle_id.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def root_span_id(cycle_id: str) -> str:
        """Deterministically derives a 16-char hex root span_id for the cycle, matching data_trace.root_span."""
        return hashlib.sha256((cycle_id + ":root").encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def generate_span_id() -> str:
        return uuid.uuid4().hex[:16]

    @classmethod
    def flush(cls) -> None:
        """Forces immediate export of all queued spans to the collector."""
        get_exporter().flush()

    @classmethod
    def emit_span(
        cls,
        cycle_id: str,
        ticker: str,
        stage: str,
        status: str = "OK",
        parent_span_id: Optional[str] = None,
        span_id: Optional[str] = None,
        duration_ms: Optional[int] = 0,
        links: Optional[list[dict[str, str]]] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        trace_id = cls.derive_trace_id(cycle_id)
        final_span_id = span_id or cls.generate_span_id()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        span = TradingSpan(
            trace_id=trace_id,
            span_id=final_span_id,
            parent_span_id=parent_span_id,
            cycle_id=cycle_id,
            ticker=ticker,
            stage=stage,
            status=status,
            start_time=now_iso,
            end_time=now_iso,
            duration_ms=duration_ms,
            attributes=attributes or {},
            links=links or [],
        )

        try:
            get_exporter().enqueue(span)
        except Exception as e:
            logger.debug("[LineageTracker] Failed to enqueue span: %s", e)

        return span

    @classmethod
    def record_cycle_start(
        cls, cycle_id: str, scope: str = "production", attributes: Optional[dict[str, Any]] = None
    ) -> TradingSpan:
        cls._cycle_start_times[cycle_id] = time.monotonic()
        attrs = {"cycle_id": cycle_id, "scope": scope}
        if attributes:
            attrs.update(attributes)
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker="PORTFOLIO",
            stage="trading.cycle",
            status="OK",
            span_id=cls.root_span_id(cycle_id),
            parent_span_id=None,
            attributes=attrs,
        )

    @classmethod
    def record_cycle_end(
        cls,
        cycle_id: str,
        status: str = "OK",
        error: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        start_t = cls._cycle_start_times.pop(cycle_id, None)
        duration_ms = int((time.monotonic() - start_t) * 1000) if start_t is not None else 0
        attrs = {"cycle_id": cycle_id}
        if error:
            attrs["error"] = error
        if attributes:
            attrs.update(attributes)
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker="PORTFOLIO",
            stage="trading.cycle",
            status=status,
            span_id=cls.root_span_id(cycle_id),
            parent_span_id=None,
            duration_ms=duration_ms,
            attributes=attrs,
        )

    @classmethod
    def record_market_snapshot(
        cls, cycle_id: str, ticker: str, bar_price: float, source: str, parent_span_id: Optional[str] = None
    ) -> TradingSpan:
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="market_snapshot",
            parent_span_id=parent_span_id or (cls.root_span_id(cycle_id) if cycle_id != "market-feed" else None),
            attributes={"price": bar_price, "source": source},
        )

    @classmethod
    def record_market_snapshot_consumed(
        cls,
        cycle_id: str,
        ticker: str,
        bar_price: float,
        source: str = "cache",
        parent_span_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        attrs = {"price": bar_price, "source": source, "consumed_by_cycle": cycle_id}
        if attributes:
            attrs.update(attributes)
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="market_snapshot",
            parent_span_id=parent_span_id or cls.root_span_id(cycle_id),
            attributes=attrs,
        )

    @classmethod
    def record_decision(
        cls,
        cycle_id: str,
        ticker: str,
        decision_id: str,
        action: str,
        confidence: int,
        parent_span_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        attrs = {"decision_id": decision_id, "action": action, "confidence": confidence}
        if attributes:
            attrs.update(attributes)
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="decision",
            parent_span_id=parent_span_id or cls.root_span_id(cycle_id),
            attributes=attrs,
        )

    @classmethod
    def record_policy_eval(
        cls,
        cycle_id: str,
        ticker: str,
        policy_decision_id: str,
        decision_id: str,
        verdict: str,
        approved: bool,
        parent_span_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        domain_result = "APPROVED" if approved else "REJECTED"
        attrs = {
            "policy_decision_id": policy_decision_id,
            "decision_id": decision_id,
            "verdict": verdict,
            "approved": approved,
            "domain_result": domain_result,
        }
        if attributes:
            attrs.update(attributes)
        # Policy rejection is a normal domain result, NOT an infrastructure error (status is OK)
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="policy",
            status="OK",
            parent_span_id=parent_span_id or cls.root_span_id(cycle_id),
            attributes=attrs,
        )

    @classmethod
    def record_execution_intent(
        cls,
        cycle_id: str,
        ticker: str,
        execution_intent_id: str,
        policy_decision_id: Optional[str] = None,
        reservation_id: Optional[str] = None,
        action: str = "BUY",
        shares: int = 0,
        price: float = 0.0,
        parent_span_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        attrs = {
            "execution_intent_id": execution_intent_id,
            "policy_decision_id": policy_decision_id,
            "reservation_id": reservation_id,
            "action": action,
            "shares": shares,
            "target_price": price,
        }
        if attributes:
            attrs.update(attributes)
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="execution_intent",
            parent_span_id=parent_span_id or cls.root_span_id(cycle_id),
            attributes=attrs,
        )

    @classmethod
    def record_reservation(
        cls,
        cycle_id: str,
        ticker: str,
        reservation_id: str,
        slot_key: Optional[str] = None,
        capital: float = 0.0,
        parent_span_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        attrs = {"reservation_id": reservation_id, "slot_key": slot_key, "capital_allocated": capital}
        if attributes:
            attrs.update(attributes)
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="reservation_slot",
            parent_span_id=parent_span_id or cls.root_span_id(cycle_id),
            attributes=attrs,
        )

    @classmethod
    def record_order_fill(
        cls,
        cycle_id: str,
        ticker: str,
        order_id: str,
        fill_id: str,
        execution_intent_id: Optional[str] = None,
        shares: int = 0,
        fill_price: float = 0.0,
        parent_span_id: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        attrs = {
            "order_id": order_id,
            "fill_id": fill_id,
            "execution_intent_id": execution_intent_id,
            "executed_shares": shares,
            "executed_price": fill_price,
        }
        if attributes:
            attrs.update(attributes)
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="order_fill",
            parent_span_id=parent_span_id or cls.root_span_id(cycle_id),
            attributes=attrs,
        )

    @classmethod
    def record_reconciliation(
        cls,
        cycle_id: str,
        ticker: str,
        reconciliation_id: str,
        status: str,
        diff: float = 0.0,
        reconciliation_type: str = "SHADOW",
        parent_span_id: Optional[str] = None,
        links: Optional[list[dict[str, str]]] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        attrs = {
            "reconciliation_id": reconciliation_id,
            "reconciliation_status": status,
            "domain_result": status,
            "discrepancy": diff,
            "reconciliation_type": reconciliation_type,
        }
        if attributes:
            attrs.update(attributes)
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="reconciliation",
            status="OK",
            parent_span_id=parent_span_id or cls.root_span_id(cycle_id),
            links=links or [],
            attributes=attrs,
        )

    @classmethod
    def record_outcome(
        cls,
        cycle_id: str,
        ticker: str,
        outcome_id: str,
        outcome: str,
        pnl_pct: float = 0.0,
        is_shadow: bool = False,
        parent_span_id: Optional[str] = None,
        links: Optional[list[dict[str, str]]] = None,
        attributes: Optional[dict[str, Any]] = None,
    ) -> TradingSpan:
        attrs = {
            "outcome_id": outcome_id,
            "outcome": outcome,
            "domain_result": outcome,
            "pnl_pct": pnl_pct,
            "is_shadow": is_shadow,
        }
        if attributes:
            attrs.update(attributes)
        # Trading loss is a normal domain result, NOT an infrastructure error
        return cls.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="shadow_outcome" if is_shadow else "outcome",
            status="OK",
            parent_span_id=parent_span_id,
            links=links or [],
            attributes=attrs,
        )


class CandidateTradingObservation:
    """Creates trace-backed candidate observations for verified trading failures or outcomes."""

    @staticmethod
    def create_candidate(
        cycle_id: str,
        ticker: str,
        lesson_text: str,
        evidence_ref: str,
        round_num: int = 1,
    ) -> dict[str, Any]:
        trace_id = TradingLineageTracker.derive_trace_id(cycle_id)
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        obs_id = f"cand_{uuid.uuid4().hex[:12]}"

        record = {
            "id": obs_id,
            "lifecycle_state": "CANDIDATE",
            "cycle_id": cycle_id,
            "trace_id": trace_id,
            "ticker": ticker.upper().strip(),
            "lesson_text": lesson_text,
            "evidence_ref": evidence_ref,
            "round": round_num,
            "created_at": now_iso,
            "verified_at": None,
        }

        # Also emit a span for the observation creation
        TradingLineageTracker.emit_span(
            cycle_id=cycle_id,
            ticker=ticker,
            stage="candidate_observation",
            attributes={"observation_id": obs_id, "evidence_ref": evidence_ref},
        )

        return record


def get_cycle_lineage(cycle_id: str) -> dict[str, Any]:
    """Retrieves full applicable lineage and authoritative domain IDs for a trading cycle."""
    from app.db import mongo_store

    trace_id = TradingLineageTracker.derive_trace_id(cycle_id)
    root_span_id = TradingLineageTracker.root_span_id(cycle_id)

    db = mongo_store.get_doc_db()

    # Query domain collections
    decisions = list(db["decision_artifacts"].find({"cycle_id": cycle_id}, {"_id": 0}))
    decision_ids = [d.get("decision_id") for d in decisions if d.get("decision_id")]

    policies = list(db["policy_decisions"].find({"decision_id": {"$in": decision_ids}}, {"_id": 0})) if decision_ids else []
    reservations = list(db["risk_reservations"].find({"cycle_id": cycle_id}, {"_id": 0}))
    intents = list(db["execution_intents"].find({"cycle_id": cycle_id}, {"_id": 0}))
    intent_ids = [i.get("execution_intent_id") for i in intents if i.get("execution_intent_id")]

    order_attempts = list(db["order_attempts"].find({"execution_intent_id": {"$in": intent_ids}}, {"_id": 0})) if intent_ids else []
    fills = list(db["trade_fills"].find({"execution_intent_id": {"$in": intent_ids}}, {"_id": 0})) if intent_ids else []
    reconciliations = list(db["execution_reconciliations"].find({"execution_intent_id": {"$in": intent_ids}}, {"_id": 0})) if intent_ids else []
    outcomes = list(db["decision_outcomes"].find({"decision_id": {"$in": decision_ids}}, {"_id": 0})) if decision_ids else []
    events = list(db["pipeline_trace_events"].find({"cycle_id": cycle_id}, {"_id": 0}))

    # Also include any un-flushed spans in the exporter queue
    queued_spans = [
        s for s in list(get_exporter().queue)
        if s.get("cycle_id") == cycle_id
    ]

    return {
        "cycle_id": cycle_id,
        "trace_id": trace_id,
        "root_span_id": root_span_id,
        "decisions": decisions,
        "policies": policies,
        "reservations": reservations,
        "intents": intents,
        "order_attempts": order_attempts,
        "fills": fills,
        "reconciliations": reconciliations,
        "outcomes": outcomes,
        "events": events,
        "queued_spans": queued_spans,
    }
