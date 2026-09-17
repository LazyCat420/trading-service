"""Unit tests for scheduled mature outcome evaluation and attribution worker."""

import datetime
import pytest

from app.trading.attribution.models import (
    AttributionClass,
    DecisionArtifact,
    OutcomeMaturityStatus,
)
from app.trading.attribution.worker import (
    evaluate_decision_at_horizon,
    run_mature_outcome_evaluation_iteration,
)


def test_evaluate_decision_at_horizon_mature(monkeypatch):
    """Mature decision evaluates return and alpha against benchmark and generates attribution."""
    now = datetime.datetime(2026, 9, 16, 22, 0, 0, tzinfo=datetime.timezone.utc)
    entry_time = now - datetime.timedelta(days=7, hours=1)

    artifact = DecisionArtifact(
        decision_id="dec-mature-1",
        cycle_id="cycle-1",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=85,
        declared_horizon_days=7,
        benchmark_symbol="SPY",
        reference_quote={"price": 100.0},
        created_at=entry_time,
    )

    outcomes_db = {}
    attribution_db = {}

    class MockCursor:
        def __init__(self, items):
            self._items = list(items)

        def sort(self, *args, **kwargs):
            return self

        def skip(self, count):
            self._items = self._items[count:]
            return self

        def limit(self, count):
            self._items = self._items[:count]
            return self

        def __iter__(self):
            return iter(self._items)

    class MockCollection:
        def __init__(self, storage, key_field):
            self.storage = storage
            self.key_field = key_field

        def find(self, filt=None, projection=None, session=None):
            items = list(self.storage.values())
            if filt:
                target_key = filt.get(self.key_field)
                if target_key is not None:
                    items = [d for d in items if d.get(self.key_field) == target_key]
            return MockCursor(items)

        def find_one(self, filt, session=None):
            target_key = filt.get(self.key_field) if isinstance(filt, dict) else None
            return self.storage.get(target_key)

        def update_one(self, filt, update, upsert=False, session=None):
            key = filt.get(self.key_field)
            doc = update.get("$set", {})
            if key in self.storage:
                self.storage[key].update(doc)
            else:
                self.storage[key] = dict(doc)

        def insert_one(self, doc, *args, **kwargs):
            key = doc.get(self.key_field)
            self.storage[key] = dict(doc)

        def insert_many(self, docs, *args, **kwargs):
            class InsertManyResult:
                def __init__(self, ids):
                    self.inserted_ids = ids
            ids = []
            for doc in docs:
                key = doc.get(self.key_field)
                self.storage[key] = dict(doc)
                ids.append(key)
            return InsertManyResult(ids)

        def create_index(self, *args, **kwargs):
            pass

    monkeypatch.setattr(
        "app.db.mongo_store.get_doc_db",
        lambda: {
            "decision_outcomes": MockCollection(outcomes_db, "outcome_id"),
            "attribution_reports": MockCollection(attribution_db, "attribution_id"),
        },
    )

    # AAPL went from 100 -> 110 (+10%)
    monkeypatch.setattr("app.trading.attribution.worker._get_current_price", lambda s: (110.0, 0.5))

    # SPY went from 400 -> 420 (+5%)
    def mock_bm_price(symbol, target_dt):
        if target_dt <= entry_time + datetime.timedelta(hours=1):
            return 400.0
        return 420.0

    monkeypatch.setattr("app.trading.attribution.worker._get_benchmark_price", mock_bm_price)

    outcome = evaluate_decision_at_horizon(artifact, now=now)
    assert outcome is not None
    assert outcome.maturity_status == OutcomeMaturityStatus.MATURE
    assert outcome.decision_return == 10.0
    assert outcome.benchmark_return == 5.0
    assert outcome.decision_alpha == 5.0

    # Attribution report was generated
    assert len(attribution_db) == 1
    report = list(attribution_db.values())[0]
    assert report["classification"] == AttributionClass.NO_FAILURE.value
    assert report["primary_reason_code"] == "ALPHA_POSITIVE"


def test_evaluate_decision_missing_benchmark_unresolved(monkeypatch):
    """Missing benchmark produces reasoned UNRESOLVED status without fabricated alpha."""
    now = datetime.datetime(2026, 9, 16, 22, 0, 0, tzinfo=datetime.timezone.utc)
    entry_time = now - datetime.timedelta(days=8)

    artifact = DecisionArtifact(
        decision_id="dec-unres-1",
        cycle_id="cycle-1",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=85,
        declared_horizon_days=7,
        benchmark_symbol="SPY",
        reference_quote={"price": 100.0},
        created_at=entry_time,
    )

    outcomes_db = {}
    attribution_db = {}

    class MockCollection:
        def __init__(self, storage, key_field):
            self.storage = storage
            self.key_field = key_field

        def find_one(self, filt, session=None):
            return self.storage.get(filt.get(self.key_field))

        def update_one(self, filt, update, upsert=False, session=None):
            key = filt.get(self.key_field)
            doc = update.get("$set", {})
            self.storage[key] = dict(doc)

    monkeypatch.setattr(
        "app.db.mongo_store.get_doc_db",
        lambda: {
            "decision_outcomes": MockCollection(outcomes_db, "outcome_id"),
            "attribution_reports": MockCollection(attribution_db, "attribution_id"),
        },
    )

    monkeypatch.setattr("app.trading.attribution.worker._get_current_price", lambda s: (110.0, 0.5))
    # Benchmark returns None (missing market data)
    monkeypatch.setattr("app.trading.attribution.worker._get_benchmark_price", lambda s, dt: None)

    outcome = evaluate_decision_at_horizon(artifact, now=now)
    assert outcome is not None
    assert outcome.maturity_status == OutcomeMaturityStatus.UNRESOLVED
    assert outcome.decision_alpha is None
    assert outcome.benchmark_return is None


def test_evaluate_immature_decision_skipped():
    """Decisions younger than declared horizon are skipped until maturity."""
    now = datetime.datetime(2026, 9, 16, 22, 0, 0, tzinfo=datetime.timezone.utc)
    entry_time = now - datetime.timedelta(days=2)  # Only 2 days old, horizon is 7

    artifact = DecisionArtifact(
        decision_id="dec-young-1",
        cycle_id="cycle-1",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=85,
        declared_horizon_days=7,
        reference_quote={"price": 100.0},
        created_at=entry_time,
    )

    outcome = evaluate_decision_at_horizon(artifact, now=now)
    assert outcome is None
