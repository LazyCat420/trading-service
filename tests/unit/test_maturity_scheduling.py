"""Unit tests for Step 08: Maturity scheduling, starvation prevention, and recovery."""

import datetime
import secrets
import pytest

from app.trading.attribution.models import (
    AttributionClass,
    DecisionArtifact,
    OutcomeMaturityStatus,
)
from app.trading.attribution.outcome_contract import PriceObservation
from app.trading.attribution.repository import (
    COLL_ATTRIBUTION_REPORTS,
    COLL_DECISION_ARTIFACTS,
    COLL_DECISION_OUTCOMES,
    COLL_DECISION_QUARANTINE,
    COLL_EVALUATION_CHECKPOINTS,
    COLL_EXECUTION_INTENTS,
    get_evaluation_checkpoint,
)
from app.trading.attribution.worker import (
    MAX_OUTCOME_RETRIES,
    evaluate_decision_at_horizon,
    run_mature_outcome_evaluation_iteration,
)


class MockCursor:
    def __init__(self, items):
        self._items = list(items)

    def sort(self, sort_spec):
        # Handle list of (field, direction)
        if isinstance(sort_spec, list) and sort_spec:
            for field, direction in reversed(sort_spec):
                reverse = direction < 0
                # Python sorting with None handling: None sorts first in asc
                self._items.sort(
                    key=lambda d: (
                        (0, d.get(field))
                        if d.get(field) is not None
                        else (-1 if not reverse else 1, "")
                    ),
                    reverse=reverse,
                )
        return self

    def limit(self, count):
        self._items = self._items[:count]
        return self

    def __iter__(self):
        return iter(self._items)


class MockCollection:
    def __init__(self, storage, key_field="_id"):
        self.storage = storage
        self.key_field = key_field

    def _matches(self, doc, filt):
        if not filt:
            return True
        for k, v in filt.items():
            if k == "$and":
                if not all(self._matches(doc, sub) for sub in v):
                    return False
            elif k == "$or":
                if not any(self._matches(doc, sub) for sub in v):
                    return False
            elif isinstance(v, dict):
                val = doc.get(k)
                if "$ne" in v and val == v["$ne"]:
                    return False
                if "$nin" in v and val in v["$nin"]:
                    return False
                if "$lte" in v:
                    if val is None or val > v["$lte"]:
                        return False
                if "$exists" in v:
                    exists = k in doc
                    if v["$exists"] != exists:
                        return False
            else:
                if doc.get(k) != v:
                    return False
        return True

    def find(self, filt=None, projection=None, session=None):
        items = [d for d in self.storage.values() if self._matches(d, filt)]
        return MockCursor(items)

    def find_one(self, filt, session=None):
        items = [d for d in self.storage.values() if self._matches(d, filt)]
        return items[0] if items else None

    def update_one(self, filt, update, upsert=False, session=None):
        matching = [d for d in self.storage.values() if self._matches(d, filt)]
        if matching:
            target = matching[0]
            if "$set" in update:
                target.update(update["$set"])
            return
        if upsert:
            new_doc = dict(filt)
            if "$set" in update:
                new_doc.update(update["$set"])
            key = new_doc.get(self.key_field) or secrets.token_hex(8)
            new_doc[self.key_field] = key
            self.storage[key] = new_doc

    def insert_one(self, doc, *args, **kwargs):
        key = doc.get(self.key_field) or secrets.token_hex(8)
        doc[self.key_field] = key
        self.storage[key] = dict(doc)

    def insert_many(self, docs, *args, **kwargs):
        class InsertManyResult:
            def __init__(self, ids):
                self.inserted_ids = ids
        ids = []
        for doc in docs:
            self.insert_one(doc)
            ids.append(doc.get(self.key_field))
        return InsertManyResult(ids)

    def create_index(self, *args, **kwargs):
        pass


@pytest.fixture
def mock_db_env(monkeypatch):
    storage = {
        COLL_DECISION_ARTIFACTS: {},
        COLL_DECISION_OUTCOMES: {},
        COLL_ATTRIBUTION_REPORTS: {},
        COLL_DECISION_QUARANTINE: {},
        COLL_EVALUATION_CHECKPOINTS: {},
        COLL_EXECUTION_INTENTS: {},
        "price_history": {},
        "worker_heartbeats": {},
    }

    db_map = {
        k: MockCollection(storage[k], key_field="decision_id" if "decision" in k else "_id")
        for k in storage
    }
    db_map[COLL_DECISION_OUTCOMES].key_field = "outcome_id"
    db_map[COLL_ATTRIBUTION_REPORTS].key_field = "attribution_id"
    db_map[COLL_EVALUATION_CHECKPOINTS].key_field = "worker"

    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: db_map)

    # Mock market data by default
    monkeypatch.setattr(
        "app.trading.attribution.worker.get_source_pinned_observation",
        lambda ticker, dt, pinned_source=None: PriceObservation(price=150.0, date=dt, source=pinned_source or "alpaca"),
    )
    monkeypatch.setattr(
        "app.trading.attribution.worker._get_benchmark_price",
        lambda symbol, dt, pinned_source=None: 450.0,
    )

    return storage


def test_mixed_horizons_scheduling(mock_db_env):
    """Verifies that a 1-day mature record is evaluated while 7-day and 30-day immature records wait."""
    now = datetime.datetime(2026, 9, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Record 1: 1-day horizon, created 2 days ago -> MATURE (due yesterday)
    art_1d = DecisionArtifact(
        decision_id="dec-1d",
        cycle_id="c1",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=80,
        declared_horizon_days=1,
        created_at=now - datetime.timedelta(days=2),
        reference_quote={"price": 140.0, "source": "alpaca"},
    )
    mock_db_env[COLL_DECISION_ARTIFACTS][art_1d.decision_id] = art_1d.model_dump(mode="python")

    # Record 2: 7-day horizon, created 4 days ago -> IMMATURE (matures in 3 days)
    art_7d = DecisionArtifact(
        decision_id="dec-7d",
        cycle_id="c1",
        ticker="MSFT",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=80,
        declared_horizon_days=7,
        created_at=now - datetime.timedelta(days=4),
        reference_quote={"price": 300.0, "source": "alpaca"},
    )
    mock_db_env[COLL_DECISION_ARTIFACTS][art_7d.decision_id] = art_7d.model_dump(mode="python")

    # Record 3: 30-day horizon, created 10 days ago -> IMMATURE (matures in 20 days)
    art_30d = DecisionArtifact(
        decision_id="dec-30d",
        cycle_id="c1",
        ticker="GOOGL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=80,
        declared_horizon_days=30,
        created_at=now - datetime.timedelta(days=10),
        reference_quote={"price": 180.0, "source": "alpaca"},
    )
    mock_db_env[COLL_DECISION_ARTIFACTS][art_30d.decision_id] = art_30d.model_dump(mode="python")

    evaluated_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert evaluated_count == 1

    # Check that dec-1d is MATURE in artifacts
    assert mock_db_env[COLL_DECISION_ARTIFACTS]["dec-1d"]["outcome_status"] == OutcomeMaturityStatus.MATURE.value

    # Check that dec-7d and dec-30d were untouched
    assert mock_db_env[COLL_DECISION_ARTIFACTS]["dec-7d"]["outcome_status"] is None
    assert mock_db_env[COLL_DECISION_ARTIFACTS]["dec-30d"]["outcome_status"] is None


def test_older_ineligible_records_do_not_starve_due_work(mock_db_env):
    """Verifies that 60 older immature records do NOT starve a newer due 1-day record."""
    now = datetime.datetime(2026, 9, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Insert 60 older records (created 15 days ago, declared_horizon_days=30 -> matures at day 30, so immature)
    for i in range(60):
        art = DecisionArtifact(
            decision_id=f"dec-old-{i:03d}",
            cycle_id="c_old",
            ticker="TSLA",
            producer="v3_decision_synthesizer",
            model="local",
            requested_action="BUY",
            confidence=75,
            declared_horizon_days=30,
            created_at=now - datetime.timedelta(days=15),
            reference_quote={"price": 200.0, "source": "alpaca"},
        )
        mock_db_env[COLL_DECISION_ARTIFACTS][art.decision_id] = art.model_dump(mode="python")

    # Insert 1 newer due record (created 2 days ago, declared_horizon_days=1 -> mature)
    due_art = DecisionArtifact(
        decision_id="dec-due-target",
        cycle_id="c_due",
        ticker="NVDA",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=90,
        declared_horizon_days=1,
        created_at=now - datetime.timedelta(days=2),
        reference_quote={"price": 120.0, "source": "alpaca"},
    )
    mock_db_env[COLL_DECISION_ARTIFACTS][due_art.decision_id] = due_art.model_dump(mode="python")

    # Run one iteration with limit=50
    evaluated_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)

    # The due target MUST be evaluated and not starved by the 60 older records!
    assert evaluated_count == 1
    assert mock_db_env[COLL_DECISION_ARTIFACTS]["dec-due-target"]["outcome_status"] == OutcomeMaturityStatus.MATURE.value


def test_malformed_records_quarantined_without_starving_valid_work(mock_db_env):
    """Verifies that malformed documents in decision_artifacts are quarantined and do not block valid work."""
    now = datetime.datetime(2026, 9, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Insert a malformed record (e.g. missing required fields or invalid types)
    malformed_doc = {
        "_id": "malformed-1",
        "decision_id": "malformed-1",
        "ticker": "INVALID_CORRUPT",
        # missing cycle_id, requested_action, confidence
        "created_at": now - datetime.timedelta(days=5),
        "declared_horizon_days": 1,
        "maturity_date": now - datetime.timedelta(days=4),
    }
    mock_db_env[COLL_DECISION_ARTIFACTS]["malformed-1"] = malformed_doc

    # Insert 2 valid due records
    for i in range(2):
        art = DecisionArtifact(
            decision_id=f"dec-valid-{i}",
            cycle_id="c_val",
            ticker="AMZN",
            producer="v3_decision_synthesizer",
            model="local",
            requested_action="BUY",
            confidence=85,
            declared_horizon_days=1,
            created_at=now - datetime.timedelta(days=2),
            reference_quote={"price": 175.0, "source": "alpaca"},
        )
        mock_db_env[COLL_DECISION_ARTIFACTS][art.decision_id] = art.model_dump(mode="python")

    # Run iteration
    evaluated_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert evaluated_count == 2

    # Malformed record must be marked QUARANTINED in artifacts
    quar_art = mock_db_env[COLL_DECISION_ARTIFACTS]["malformed-1"]
    assert quar_art["is_quarantined"] is True
    assert quar_art["outcome_status"] == "QUARANTINED"
    assert "VALIDATION_ERROR" in quar_art["quarantine_reason"]

    # Entry must exist in decision_quarantine collection
    quar_entry = mock_db_env[COLL_DECISION_QUARANTINE].get("malformed-1")
    assert quar_entry is not None
    assert "VALIDATION_ERROR" in quar_entry["reason"]

    # Second poll iteration: 0 records evaluated, malformed record is never re-selected
    second_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert second_count == 0


def test_bounded_retries_and_quarantine_on_max_retries(mock_db_env, monkeypatch):
    """Verifies exponential retry backoff and quarantine upon reaching MAX_OUTCOME_RETRIES."""
    now = datetime.datetime(2026, 9, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Force missing benchmark so evaluation produces UNRESOLVED
    monkeypatch.setattr("app.trading.attribution.worker._get_benchmark_price", lambda s, dt, pinned_source=None: None)

    art = DecisionArtifact(
        decision_id="dec-failing-retry",
        cycle_id="c_fail",
        ticker="META",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=80,
        declared_horizon_days=1,
        created_at=now - datetime.timedelta(days=2),
        reference_quote={"price": 500.0, "source": "alpaca"},
    )
    mock_db_env[COLL_DECISION_ARTIFACTS][art.decision_id] = art.model_dump(mode="python")

    # Run attempts 1 to 4: should back off
    current_time = now
    for attempt in range(1, MAX_OUTCOME_RETRIES):
        run_mature_outcome_evaluation_iteration(limit=50, now=current_time)
        doc = mock_db_env[COLL_DECISION_ARTIFACTS][art.decision_id]
        assert doc["retry_count"] == attempt
        assert doc["outcome_status"] == OutcomeMaturityStatus.UNRESOLVED.value
        assert doc["is_quarantined"] is False
        assert doc["retry_after"] > current_time
        # Advance time past the backoff for next attempt
        current_time = doc["retry_after"] + datetime.timedelta(seconds=1)

    # Attempt 5: MAX_OUTCOME_RETRIES reached -> must quarantine and mark EXCLUDED
    run_mature_outcome_evaluation_iteration(limit=50, now=current_time)
    final_doc = mock_db_env[COLL_DECISION_ARTIFACTS][art.decision_id]
    assert final_doc["retry_count"] == MAX_OUTCOME_RETRIES
    assert final_doc["is_quarantined"] is True
    assert final_doc["outcome_status"] == "EXCLUDED"
    assert "MAX_RETRIES_EXCEEDED" in final_doc["quarantine_reason"]

    # Must be in decision_quarantine
    quar_entry = mock_db_env[COLL_DECISION_QUARANTINE].get(art.decision_id)
    assert quar_entry is not None
    assert "MAX_RETRIES_EXCEEDED" in quar_entry["reason"]

    # Further iterations at any time in future must NOT select this record
    current_time += datetime.timedelta(days=10)
    assert run_mature_outcome_evaluation_iteration(limit=50, now=current_time) == 0


def test_crash_recovery_and_idempotent_duplicate_evaluation(mock_db_env):
    """Verifies that a crash between writing outcome and updating artifact recovers idempotently."""
    now = datetime.datetime(2026, 9, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)

    art = DecisionArtifact(
        decision_id="dec-crash-rec",
        cycle_id="c_crash",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=88,
        declared_horizon_days=1,
        created_at=now - datetime.timedelta(days=2),
        reference_quote={"price": 150.0, "source": "alpaca"},
    )
    mock_db_env[COLL_DECISION_ARTIFACTS][art.decision_id] = art.model_dump(mode="python")

    # Simulate pre-existing mature outcome (as if previous process crashed before updating artifact)
    outcome_id = f"out-{art.decision_id}-v3"
    mock_db_env[COLL_DECISION_OUTCOMES][outcome_id] = {
        "outcome_id": outcome_id,
        "decision_id": art.decision_id,
        "horizon_days": 1,
        "benchmark_symbol": "SPY",
        "entry_observation": {"price": 150.0, "source": "alpaca"},
        "maturity_status": OutcomeMaturityStatus.MATURE.value,
        "decision_return": 5.0,
        "benchmark_return": 2.0,
        "decision_alpha": 3.0,
        "resolved_at": now - datetime.timedelta(hours=1),
    }

    # Run evaluation iteration
    evaluated_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert evaluated_count == 1

    # Artifact must be marked MATURE
    doc = mock_db_env[COLL_DECISION_ARTIFACTS][art.decision_id]
    assert doc["outcome_status"] == OutcomeMaturityStatus.MATURE.value

    # Subsequent run: 0 records evaluated
    assert run_mature_outcome_evaluation_iteration(limit=50, now=now) == 0


def test_frozen_time_boundaries_and_checkpoints(mock_db_env):
    """Verifies frozen-time boundary evaluations and checkpoint persistence."""
    t0 = datetime.datetime(2026, 9, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)

    art = DecisionArtifact(
        decision_id="dec-checkpoint",
        cycle_id="c_ckpt",
        ticker="SPY",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=80,
        declared_horizon_days=1,
        created_at=t0,
        reference_quote={"price": 400.0, "source": "alpaca"},
    )
    mock_db_env[COLL_DECISION_ARTIFACTS][art.decision_id] = art.model_dump(mode="python")

    # Before maturity (t0 + 12 hours): should not evaluate
    assert run_mature_outcome_evaluation_iteration(limit=50, now=t0 + datetime.timedelta(hours=12)) == 0

    # At maturity (t0 + 25 hours): evaluates exactly 1
    eval_time = t0 + datetime.timedelta(hours=25)
    assert run_mature_outcome_evaluation_iteration(limit=50, now=eval_time) == 1

    # Checkpoint was recorded
    ckpt = mock_db_env[COLL_EVALUATION_CHECKPOINTS].get("outcome_worker")
    assert ckpt is not None
    assert ckpt["records_processed"] == 1
    assert ckpt["last_evaluated_at"] == eval_time


def test_legacy_record_maturity_date_backfill(mock_db_env):
    """Verifies that legacy records without maturity_date are backfilled and handled correctly."""
    now = datetime.datetime(2026, 9, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Legacy record: no maturity_date field in Mongo, declared_horizon_days=1
    legacy_doc = {
        "_id": "dec-legacy-1",
        "decision_id": "dec-legacy-1",
        "cycle_id": "c_leg",
        "ticker": "AAPL",
        "producer": "v3",
        "model": "local",
        "requested_action": "BUY",
        "confidence": 80,
        "declared_horizon_days": 1,
        "benchmark_symbol": "SPY",
        "reference_quote": {"price": 100.0, "source": "alpaca"},
        "created_at": now - datetime.timedelta(days=2),
    }
    mock_db_env[COLL_DECISION_ARTIFACTS]["dec-legacy-1"] = legacy_doc

    evaluated_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert evaluated_count == 1

    # Verify maturity_date was backfilled in Mongo
    saved_doc = mock_db_env[COLL_DECISION_ARTIFACTS]["dec-legacy-1"]
    assert saved_doc.get("maturity_date") is not None
    assert saved_doc["outcome_status"] == OutcomeMaturityStatus.MATURE.value
