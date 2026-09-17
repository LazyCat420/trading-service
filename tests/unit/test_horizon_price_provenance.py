"""Unit test suite for Step 07: Horizon Price Provenance.

Verifies all Exit Gate requirements:
1. Weekends and holidays handling within calendar-aware age tolerance.
2. Old-only history beyond age tolerance returns None and remains UNRESOLVED.
3. Strict prohibition of current-quote substitution: A missing horizon bar
   plus a valid current quote MUST NOT resolve.
4. Corporate action and unadjusted split detection: Marks outcome EXCLUDED with
   CORPORATE_ACTION_UNADJUSTED to prevent fabricated alpha.
5. Missing asset or benchmark bars produce explicit reasoned UNRESOLVED states.
6. Mixed sources rejection: Entry source pinned; conflicting horizon sources rejected.
7. Delayed processing and deterministic replay: Identical observations and vendor hashes.
8. Repeated evaluation idempotency.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch
import pytest

from app.trading.attribution.models import (
    DecisionArtifact,
    DecisionOutcomeRecord,
    OutcomeMaturityStatus,
)
from app.trading.attribution.outcome_contract import (
    AdjustmentConvention,
    BenchmarkSpec,
    ExclusionReason,
    MarketCalendar,
    MaturityStatus,
    PriceObservation,
)
from app.trading.attribution.provenance import (
    DEFAULT_MAX_AGE_DAYS_EQUITY,
    get_source_pinned_observation,
)
from app.trading.attribution.worker import (
    COLL_DECISION_ARTIFACTS,
    COLL_DECISION_OUTCOMES,
    _get_asset_historical_price,
    _get_benchmark_price,
    evaluate_decision_at_horizon,
)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs) if docs else []

    def find_one(self, query, *args, **kwargs):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    def update_one(self, filter_q, update_q, upsert=False):
        set_vals = update_q.get("$set", {})
        for d in self.docs:
            if all(d.get(k) == v for k, v in filter_q.items()):
                d.update(set_vals)
                return
        if upsert:
            new_doc = {**filter_q, **set_vals}
            self.docs.append(new_doc)

    def insert_one(self, doc, *args, **kwargs):
        self.docs.append(dict(doc))
        class Res:
            inserted_id = "mock_id"
        return Res()

    def insert_many(self, docs, *args, **kwargs):
        for d in docs:
            self.docs.append(dict(d))
        class Res:
            inserted_ids = ["mock_id"] * len(docs)
        return Res()

    def create_index(self, *args, **kwargs):
        return "idx_mock"


class FakeMongoStore:
    def __init__(self, price_history_rows=None):
        self.price_history_rows = list(price_history_rows) if price_history_rows else []
        self.collections = {
            COLL_DECISION_OUTCOMES: FakeCollection(),
            COLL_DECISION_ARTIFACTS: FakeCollection(),
            "attribution_reports": FakeCollection(),
            "execution_intents": FakeCollection(),
        }

    def get_doc_db(self):
        return self.collections

    def find_docs(self, collection_name, query, sort=None, limit=None, *args, **kwargs):
        if collection_name != "price_history":
            return self.collections.get(collection_name, FakeCollection()).docs

        res = []
        for r in self.price_history_rows:
            match = True
            for k, v in query.items():
                if isinstance(v, dict):
                    val = r.get(k)
                    if "$gte" in v and (val is None or val < v["$gte"]):
                        match = False
                    if "$lte" in v and (val is None or val > v["$lte"]):
                        match = False
                    if "$gt" in v and (val is None or val <= v["$gt"]):
                        match = False
                elif r.get(k) != v:
                    match = False
            if match:
                res.append(dict(r))

        if sort:
            # simple sorting support for date desc
            res.sort(key=lambda x: x.get("date") or x.get("timestamp"), reverse=True)
        if limit:
            res = res[:limit]
        return res


def test_weekend_and_holiday_calendar_tolerance():
    """Weekends and market holidays resolve to the preceding valid completed bar within tolerance."""
    # Target date: Sunday 2026-09-06 at 12:00 UTC
    target_sunday = datetime.datetime(2026, 9, 6, 12, 0, tzinfo=datetime.timezone.utc)
    # Friday close bar: 2026-09-04 at 00:00 UTC (completed Friday bar)
    friday_bar_date = datetime.datetime(2026, 9, 4, 0, 0, tzinfo=datetime.timezone.utc)

    fake_rows = [
        {
            "ticker": "AAPL",
            "close": 155.0,
            "date": friday_bar_date,
            "source": "alpaca",
            "adjustment_convention": "SPLIT_ADJUSTED",
        }
    ]
    fake_store = FakeMongoStore(price_history_rows=fake_rows)

    with patch("app.db.mongo_store.find_docs", side_effect=fake_store.find_docs):
        obs = get_source_pinned_observation(
            ticker="AAPL",
            target_dt=target_sunday,
            pinned_source="alpaca",
            calendar=MarketCalendar.US_EQUITY,
            max_age_days=DEFAULT_MAX_AGE_DAYS_EQUITY,
        )
        assert obs is not None
        assert obs.price == 155.0
        assert obs.source == "alpaca"
        assert obs.date == friday_bar_date
        assert obs.adjustment_convention == AdjustmentConvention.SPLIT_ADJUSTED
        assert obs.vendor_hash != ""


def test_old_only_history_beyond_tolerance_returns_none():
    """Bars older than max_age_days (e.g. stale 30-day old data) must not be accepted."""
    target_dt = datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc)
    # Old bar from 25 days ago: 2026-08-25
    old_bar_date = datetime.datetime(2026, 8, 25, 20, 0, tzinfo=datetime.timezone.utc)

    fake_rows = [
        {
            "ticker": "AAPL",
            "close": 140.0,
            "date": old_bar_date,
            "source": "alpaca",
        }
    ]
    fake_store = FakeMongoStore(price_history_rows=fake_rows)

    with patch("app.db.mongo_store.find_docs", side_effect=fake_store.find_docs):
        obs = get_source_pinned_observation(
            ticker="AAPL",
            target_dt=target_dt,
            pinned_source="alpaca",
            max_age_days=5,
        )
        assert obs is None


def test_missing_horizon_bar_with_valid_current_quote_must_not_resolve():
    """Exit Gate Check: A missing horizon bar plus a valid current quote MUST NOT resolve!"""
    entry_time = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.timezone.utc)
    maturity_date = entry_time + datetime.timedelta(days=7)  # 2026-08-08
    eval_time = datetime.datetime(2026, 9, 1, 10, 0, tzinfo=datetime.timezone.utc)

    artifact = DecisionArtifact(
        decision_id="dec-no-live-fallback-1",
        cycle_id="cycle-1",
        ticker="TSLA",
        requested_action="BUY",
        reference_quote={"price": 200.0, "source": "alpaca"},
        producer="test_agent",
        model="test_model",
        confidence=80,
        created_at=entry_time,
        declared_horizon_days=7,
        benchmark_symbol="SPY",
    )

    fake_store = FakeMongoStore(price_history_rows=[])  # Empty price history: missing horizon bar

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_store.get_doc_db()), \
         patch("app.db.mongo_store.find_docs", return_value=[]), \
         patch("app.trading.attribution.worker._get_current_price", return_value=(250.0, "alpaca")):

        outcome = evaluate_decision_at_horizon(artifact, now=eval_time)

        # MUST NOT RESOLVE TO MATURE!
        assert outcome is not None
        assert outcome.maturity_status == OutcomeMaturityStatus.UNRESOLVED
        assert outcome.decision_return is None
        assert outcome.decision_alpha is None

        # Check stored record in DB
        stored = fake_store.collections[COLL_DECISION_OUTCOMES].find_one({"outcome_id": outcome.outcome_id})
        assert stored["maturity_status"] == "UNRESOLVED"
        assert stored["exclusion_reason"] == ExclusionReason.STALE_HORIZON_BAR.value


def test_unadjusted_split_marks_outcome_excluded():
    """Exit Gate Check: Unadjusted splits must mark outcome EXCLUDED with CORPORATE_ACTION_UNADJUSTED."""
    entry_time = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.timezone.utc)
    maturity_date = entry_time + datetime.timedelta(days=7)
    eval_time = datetime.datetime(2026, 8, 10, 10, 0, tzinfo=datetime.timezone.utc)

    artifact = DecisionArtifact(
        decision_id="dec-split-test-1",
        cycle_id="cycle-1",
        ticker="NVDA",
        requested_action="BUY",
        reference_quote={"price": 1000.0, "source": "alpaca"},
        producer="test_agent",
        model="test_model",
        confidence=80,
        created_at=entry_time,
        declared_horizon_days=7,
        benchmark_symbol="SPY",
    )

    # Price history row with UNADJUSTED flag (e.g. 10:1 split raw close = 100.0)
    bar_date = datetime.datetime(2026, 8, 7, 0, 0, tzinfo=datetime.timezone.utc)
    fake_rows = [
        {
            "ticker": "NVDA",
            "close": 100.0,
            "date": bar_date,
            "source": "alpaca",
            "adjustment_convention": "UNADJUSTED",
            "is_split_adjusted": False,
        }
    ]
    fake_store = FakeMongoStore(price_history_rows=fake_rows)

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_store.get_doc_db()), \
         patch("app.db.mongo_store.find_docs", side_effect=fake_store.find_docs):

        outcome = evaluate_decision_at_horizon(artifact, now=eval_time)
        assert outcome is not None
        # Must be flagged CONTAMINATED/EXCLUDED, preventing -90% false loss
        assert outcome.maturity_status == OutcomeMaturityStatus.CONTAMINATED
        stored = fake_store.collections[COLL_DECISION_OUTCOMES].find_one({"outcome_id": outcome.outcome_id})
        assert stored["exclusion_reason"] == ExclusionReason.CORPORATE_ACTION_UNADJUSTED.value
        assert stored["maturity_status"] == MaturityStatus.EXCLUDED.value


def test_mixed_sources_rejection():
    """Exit Gate Check: Entry source and horizon source must match; conflicting sources reject."""
    target_dt = datetime.datetime(2026, 9, 10, 20, 0, tzinfo=datetime.timezone.utc)

    # Bar exists only from 'tiingo', but caller requests pinned 'alpaca'
    fake_rows = [
        {
            "ticker": "AAPL",
            "close": 160.0,
            "date": datetime.datetime(2026, 9, 9, 0, 0, tzinfo=datetime.timezone.utc),
            "source": "tiingo",
        }
    ]
    fake_store = FakeMongoStore(price_history_rows=fake_rows)

    with patch("app.db.mongo_store.find_docs", side_effect=fake_store.find_docs):
        obs = get_source_pinned_observation(
            ticker="AAPL",
            target_dt=target_dt,
            pinned_source="alpaca",
        )
        # Cannot accept tiingo when alpaca was pinned
        assert obs is None


def test_missing_benchmark_produces_reasoned_unresolved():
    """Exit Gate Check: Missing benchmark observation produces reasoned UNRESOLVED record."""
    entry_time = datetime.datetime(2026, 8, 1, 10, 0, tzinfo=datetime.timezone.utc)
    maturity_date = entry_time + datetime.timedelta(days=7)
    eval_time = datetime.datetime(2026, 8, 10, 10, 0, tzinfo=datetime.timezone.utc)

    artifact = DecisionArtifact(
        decision_id="dec-bm-missing-1",
        cycle_id="cycle-1",
        ticker="AAPL",
        requested_action="BUY",
        reference_quote={"price": 150.0, "source": "alpaca"},
        producer="test_agent",
        model="test_model",
        confidence=80,
        created_at=entry_time,
        declared_horizon_days=7,
        benchmark_symbol="SPY",
    )

    # Asset bar exists, but SPY benchmark bar is missing
    fake_rows = [
        {"ticker": "AAPL", "close": 160.0, "date": datetime.datetime(2026, 8, 7, 0, 0, tzinfo=datetime.timezone.utc), "source": "alpaca"},
    ]
    fake_store = FakeMongoStore(price_history_rows=fake_rows)

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_store.get_doc_db()), \
         patch("app.db.mongo_store.find_docs", side_effect=fake_store.find_docs):

        outcome = evaluate_decision_at_horizon(artifact, now=eval_time)
        assert outcome is not None
        assert outcome.maturity_status == OutcomeMaturityStatus.UNRESOLVED

        stored = fake_store.collections[COLL_DECISION_OUTCOMES].find_one({"outcome_id": outcome.outcome_id})
        assert stored["exclusion_reason"] == ExclusionReason.MISSING_BENCHMARK_BAR.value


def test_delayed_processing_and_replay_parity():
    """Exit Gate Check: Evaluating months later (replay) returns identical observations and vendor hashes."""
    entry_time = datetime.datetime(2026, 5, 1, 21, 0, tzinfo=datetime.timezone.utc)
    maturity_date = entry_time + datetime.timedelta(days=7)  # 2026-05-08 21:00 UTC (after 16:15 EDT)
    bar_date = datetime.datetime(2026, 5, 8, 0, 0, tzinfo=datetime.timezone.utc)

    fake_rows = [
        {"ticker": "MSFT", "close": 300.0, "date": bar_date, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
    ]
    fake_store = FakeMongoStore(price_history_rows=fake_rows)

    with patch("app.db.mongo_store.find_docs", side_effect=fake_store.find_docs):
        # Run 1: Evaluated on May 10
        eval_run_1 = datetime.datetime(2026, 5, 10, 12, 0, tzinfo=datetime.timezone.utc)
        obs_1 = get_source_pinned_observation(
            "MSFT", maturity_date, pinned_source="alpaca", as_of=eval_run_1
        )

        # Run 2: Replay on Sept 17
        eval_run_2 = datetime.datetime(2026, 9, 17, 12, 0, tzinfo=datetime.timezone.utc)
        obs_2 = get_source_pinned_observation(
            "MSFT", maturity_date, pinned_source="alpaca", as_of=eval_run_2
        )

        assert obs_1 is not None
        assert obs_2 is not None
        assert obs_1.price == obs_2.price == 300.0
        assert obs_1.vendor_hash == obs_2.vendor_hash
        assert obs_1.date == obs_2.date


def test_repeated_evaluation_idempotency():
    """Exit Gate Check: Repeated evaluation returns existing mature record without side effects."""
    entry_time = datetime.datetime(2026, 8, 1, 21, 0, tzinfo=datetime.timezone.utc)
    maturity_date = entry_time + datetime.timedelta(days=7)  # 2026-08-08 21:00 UTC
    eval_time = datetime.datetime(2026, 8, 10, 10, 0, tzinfo=datetime.timezone.utc)

    artifact = DecisionArtifact(
        decision_id="dec-idemp-1",
        cycle_id="cycle-1",
        ticker="AAPL",
        requested_action="BUY",
        reference_quote={"price": 150.0, "source": "alpaca"},
        producer="test_agent",
        model="test_model",
        confidence=80,
        created_at=entry_time,
        declared_horizon_days=7,
        benchmark_symbol="SPY",
    )

    entry_bar_date = datetime.datetime(2026, 8, 1, 0, 0, tzinfo=datetime.timezone.utc)
    horizon_bar_date = datetime.datetime(2026, 8, 8, 0, 0, tzinfo=datetime.timezone.utc)

    fake_rows = [
        {"ticker": "AAPL", "close": 165.0, "date": horizon_bar_date, "source": "alpaca"},
        {"ticker": "SPY", "close": 400.0, "date": entry_bar_date, "source": "alpaca"},
        {"ticker": "SPY", "close": 420.0, "date": horizon_bar_date, "source": "alpaca"},
    ]
    fake_store = FakeMongoStore(price_history_rows=fake_rows)

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_store.get_doc_db()), \
         patch("app.db.mongo_store.find_docs", side_effect=fake_store.find_docs):

        # First evaluation: evaluates and stores MATURE record
        res1 = evaluate_decision_at_horizon(artifact, now=eval_time)
        assert res1 is not None
        assert res1.maturity_status == OutcomeMaturityStatus.MATURE

        # Second evaluation: returns stored record via idempotency check
        res2 = evaluate_decision_at_horizon(artifact, now=eval_time + datetime.timedelta(days=1))
        assert res2 is not None
        assert res2.outcome_id == res1.outcome_id
        assert res2.decision_alpha == res1.decision_alpha
