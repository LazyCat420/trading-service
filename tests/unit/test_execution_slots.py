"""Unit tests for Execution Slots, Idempotency, and Supersession."""

import datetime
import pytest
from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    IntentStatus,
    PolicyDecision,
    PolicyDisposition,
)
from app.trading.attribution.repository import (
    claim_execution_slot,
    supersede_execution_intent,
)
from app.trading.policy.policy_translator import PolicyInputSnapshot, PolicyTranslator


def test_slot_key_and_stable_idempotency_key():
    """PolicyTranslator generates deterministic slot_key and SHA-256 idempotency_key."""
    now = datetime.datetime(2026, 9, 16, 22, 0, 0, tzinfo=datetime.timezone.utc)
    artifact = DecisionArtifact(
        decision_id="dec-test-1",
        cycle_id="cycle-1",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=85,
        reference_quote={"price": 150.0},
    )
    snapshot = PolicyInputSnapshot(
        portfolio_equity=100000.0,
        cash_balance=50000.0,
        quote_price=150.0,
        quote_age_hours=0.5,
        as_of=now,
    )

    _, intent1 = PolicyTranslator.evaluate(artifact, snapshot)
    assert intent1 is not None
    assert intent1.slot_key is not None
    assert "AAPL" in intent1.slot_key
    assert "dec-test-1" in intent1.slot_key
    assert len(intent1.idempotency_key) == 64  # SHA-256 hex string


def test_claim_execution_slot_lifecycle(monkeypatch):
    """Execution slots guarantee atomic claiming, idempotency, and conflict rejection."""
    slots_db = {}

    class MockSlotCollection:
        def find_one(self, filt, session=None):
            return slots_db.get(filt.get("slot_key"))

        def insert_one(self, doc, session=None):
            key = doc["slot_key"]
            if key in slots_db:
                raise Exception("DuplicateKey")
            slots_db[key] = dict(doc)

        def update_one(self, filt, update, session=None):
            key = filt["slot_key"]
            if key in slots_db:
                slots_db[key].update(update.get("$set", {}))

    monkeypatch.setattr(
        "app.db.mongo_store.get_doc_db",
        lambda: {"execution_slots": MockSlotCollection()},
    )

    slot_key = "slot:AAPL:dec-1:BUY:202609162200_202609162230"
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = now + datetime.timedelta(minutes=30)

    # 1. First claim succeeds
    ok, doc = claim_execution_slot(slot_key, "dec-1", "int-1", expires)
    assert ok is True
    assert doc["decision_id"] == "dec-1"

    # 2. Duplicate claim by same decision succeeds (idempotent retry)
    ok_retry, doc_retry = claim_execution_slot(slot_key, "dec-1", "int-1", expires)
    assert ok_retry is True
    assert doc_retry["decision_id"] == "dec-1"

    # 3. Competing distinct decision rejected
    ok_conflict, doc_conflict = claim_execution_slot(slot_key, "dec-competing-2", "int-2", expires)
    assert ok_conflict is False
    assert doc_conflict["decision_id"] == "dec-1"

    # 4. Once expired, new decision can claim
    past_expires = now - datetime.timedelta(seconds=1)
    slots_db[slot_key]["expires_at"] = past_expires
    ok_reclaim, doc_reclaim = claim_execution_slot(slot_key, "dec-newer-3", "int-3", expires)
    assert ok_reclaim is True
    assert doc_reclaim["decision_id"] == "dec-newer-3"


def test_supersede_execution_intent(monkeypatch):
    """supersede_execution_intent transitions intent status to SUPERSEDED."""
    intents_db = {
        "int-100": {"execution_intent_id": "int-100", "status": "CREATED"},
    }

    class MockIntentCollection:
        def update_one(self, filt, update, session=None):
            intent_id = filt.get("execution_intent_id")
            req_status = filt.get("status")
            if intent_id in intents_db and intents_db[intent_id].get("status") == req_status:
                intents_db[intent_id].update(update.get("$set", {}))
                class Res:
                    modified_count = 1
                return Res()
            class Res0:
                modified_count = 0
            return Res0()

    monkeypatch.setattr(
        "app.db.mongo_store.get_doc_db",
        lambda: {"execution_intents": MockIntentCollection()},
    )

    success = supersede_execution_intent("int-100", "dec-new-revision-2")
    assert success is True
    assert intents_db["int-100"]["status"] == "SUPERSEDED"
    assert intents_db["int-100"]["superseded_by"] == "dec-new-revision-2"

    # Superseding already superseded intent fails
    fail = supersede_execution_intent("int-100", "dec-new-revision-3")
    assert fail is False
