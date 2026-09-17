"""Unit tests for Step 11: Historical Data Disposition Audit & Clean-Break Classifier.

Covers:
1. classify_decision_outcome:
   - Exhaustive classification of all cohorts.
   - Enforces no-silent-upgrades invariant: only genuine verified records qualify for learning.
2. classify_lot_closure:
   - Excludes legacy pre-v4 closures missing allocated fees.
   - Qualifies complete live and reconstructed lots.
3. audit_historical_database:
   - Mathematical cohort reconciliation.
   - Fail-closed invariant verification.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock
import pytest

from app.trading.attribution.historical_classifier import (
    classify_decision_outcome,
    classify_lot_closure,
    audit_historical_database,
)


def test_classify_decision_outcome_cohorts():
    """Verify classification of decision outcomes across all known cohorts."""
    # 1. Synthetic cycle
    synth = classify_decision_outcome({"cycle_id": "bench-contamination-01", "decision_id": "d1"})
    assert synth["cohort"] == "SYNTHETIC_CYCLE"
    assert synth["learning_eligible"] is False

    # 2. Quarantined
    quar = classify_decision_outcome({"cycle_id": "c1", "is_quarantined": True, "decision_id": "d2"})
    assert quar["cohort"] == "QUARANTINED"
    assert quar["learning_eligible"] is False

    # 3. Explicitly excluded
    excl = classify_decision_outcome({"cycle_id": "c1", "exclusion_reason": "DELISTED", "decision_id": "d3"})
    assert excl["cohort"] == "EXPLICITLY_EXCLUDED"
    assert excl["learning_eligible"] is False

    # 4. Provenance unrecoverable
    unrec = classify_decision_outcome({"cycle_id": "c1", "outcome_evidence_state": "provenance_unrecoverable", "decision_id": "d4"})
    assert unrec["cohort"] == "PROVENANCE_UNRECOVERABLE"
    assert unrec["learning_eligible"] is False

    # 5. Unsupported claim
    unsupp = classify_decision_outcome({"cycle_id": "c1", "outcome_evidence_state": "unsupported_claim", "decision_id": "d5"})
    assert unsupp["cohort"] == "UNSUPPORTED_CLAIM"
    assert unsupp["learning_eligible"] is False

    # 6. Pending resolution
    pend = classify_decision_outcome({"cycle_id": "c1", "outcome_evidence_state": "pending", "decision_id": "d6"})
    assert pend["cohort"] == "PENDING_RESOLUTION"
    assert pend["learning_eligible"] is False

    # 7. Contract v4 Mature Verified
    v4_ok = classify_decision_outcome({
        "cycle_id": "c1",
        "contract_version": 4,
        "maturity_status": "MATURE_VERIFIED",
        "decision_id": "d7",
    })
    assert v4_ok["cohort"] == "CONTRACT_V4_MATURE_VERIFIED"
    assert v4_ok["learning_eligible"] is True

    # 8. Legacy Contract v2 Verified with matching vendors
    v2_ok = classify_decision_outcome({
        "cycle_id": "c1",
        "outcome_contract_version": 2,
        "outcome_evidence_state": "verified",
        "entry_price_source": "polygon",
        "exit_price_source": "polygon",
        "decision_id": "d8",
    })
    assert v2_ok["cohort"] == "CONTRACT_V2_VERIFIED"
    assert v2_ok["learning_eligible"] is True

    # 9. Legacy Contract v2 with mismatched vendors
    v2_mismatch = classify_decision_outcome({
        "cycle_id": "c1",
        "outcome_contract_version": 2,
        "outcome_evidence_state": "verified",
        "entry_price_source": "polygon",
        "exit_price_source": "yfinance",
        "decision_id": "d9",
    })
    assert v2_mismatch["cohort"] == "LEGACY_VENDOR_MISMATCH"
    assert v2_mismatch["learning_eligible"] is False

    # 10. Legacy Unversioned
    legacy_unver = classify_decision_outcome({
        "cycle_id": "c1",
        "ticker": "AAPL",
        "pnl_pct": 5.0,
        "decision_id": "d10",
    })
    assert legacy_unver["cohort"] == "LEGACY_UNVERSIONED"
    assert legacy_unver["disposition"] == "HISTORICAL_ARCHIVE"
    assert legacy_unver["learning_eligible"] is False


def test_classify_lot_closure_cohorts():
    """Verify lot closure classification and attribution eligibility."""
    # 1. Live attributable v4
    live_v4 = classify_lot_closure({
        "closure_id": "c1",
        "origin": "LIVE",
        "is_attributable": True,
        "provenance_complete": True,
        "allocated_entry_fee": 1.0,
        "invested_capital_denominator": 1001.0,
    })
    assert live_v4["cohort"] == "LIVE_ATTRIBUTABLE_V4"
    assert live_v4["attribution_eligible"] is True

    # 2. Historical reconstruction
    recon = classify_lot_closure({
        "closure_id": "c2",
        "origin": "HISTORICAL_RECONSTRUCTION",
        "is_attributable": True,
        "provenance_complete": True,
        "allocated_entry_fee": 0.0,
        "invested_capital_denominator": 500.0,
    })
    assert recon["cohort"] == "HISTORICAL_RECONSTRUCTION_ATTRIBUTABLE"
    assert recon["attribution_eligible"] is True

    # 3. Legacy pre-v4 closure (missing allocated_entry_fee or not attributable)
    leg = classify_lot_closure({
        "closure_id": "c3",
        "entry_price": 100.0,
        "exit_price": 110.0,
        "realized_pnl": 10.0,
    })
    assert leg["cohort"] == "LEGACY_UNALLOCATED_CLOSURE"
    assert leg["attribution_eligible"] is False
    assert leg["disposition"] == "HISTORICAL_ARCHIVE_EXCLUDED"


def test_audit_historical_database_reconciliation():
    """Verify audit_historical_database reconciles cohort counts and validates invariants."""
    mock_db = MagicMock()
    mock_dec = [
        {"cycle_id": "c1", "contract_version": 4, "maturity_status": "MATURE_VERIFIED", "decision_id": "d1"},
        {"cycle_id": "c2", "outcome_contract_version": 2, "outcome_evidence_state": "verified", "entry_price_source": "poly", "exit_price_source": "poly", "decision_id": "d2"},
        {"cycle_id": "bench-sim-1", "decision_id": "d3"},
        {"cycle_id": "c3", "ticker": "SPY", "pnl_pct": 1.0, "decision_id": "d4"},
    ]
    mock_lots = [
        {"closure_id": "l1", "origin": "LIVE", "is_attributable": True, "provenance_complete": True, "allocated_entry_fee": 1.0, "invested_capital_denominator": 1001.0},
        {"closure_id": "l2", "realized_pnl": 5.0},
    ]
    mock_pls = [
        {"lot_id": "p1", "status": "open"},
        {"lot_id": "p2", "status": "closed"},
    ]

    mock_db.__getitem__.side_effect = lambda name: MagicMock(
        find=lambda q: (
            mock_dec if name == "decision_outcomes"
            else (mock_lots if name == "lot_closures" else mock_pls)
        )
    )

    audit = audit_historical_database(mock_db)
    assert audit["decision_outcomes"]["total"] == 4
    assert audit["decision_outcomes"]["reconciled"] is True
    assert audit["decision_outcomes"]["no_silent_upgrades_verified"] is True
    assert audit["decision_outcomes"]["learning_eligible_count"] == 2

    assert audit["lot_closures"]["total"] == 2
    assert audit["lot_closures"]["reconciled"] is True
    assert audit["lot_closures"]["attributable_count"] == 1

    assert audit["position_lots"]["total"] == 2
    assert audit["position_lots"]["open_count"] == 1
    assert audit["position_lots"]["closed_count"] == 1
