"""Historical Data Disposition Classifier and Clean-Break Verification Engine.

Implements Step 11: Resolve historical-data treatment:
- Strict clean-break classification across decision outcomes and lot closures.
- Zero silent upgrades: unversioned, unsupported, and incomplete records are strictly quarantined.
- Reconciles cohort totals and verifies reader compatibility/exclusion paths.
"""

from __future__ import annotations

from typing import Any, Optional
from app.services.cycle_scope import is_synthetic_cycle
from app.trading.attribution.outcome_reader import _check_learning_eligibility


def classify_decision_outcome(doc: dict[str, Any]) -> dict[str, Any]:
    """Classifies a decision outcome document into a mutually exclusive disposition cohort."""
    dec_id = doc.get("decision_id") or doc.get("id") or str(doc.get("_id", ""))
    cycle_id = doc.get("cycle_id", "")
    
    # 1. Synthetic / test cycle
    if is_synthetic_cycle(cycle_id):
        return {
            "id": dec_id,
            "cohort": "SYNTHETIC_CYCLE",
            "disposition": "EXCLUDED",
            "learning_eligible": False,
            "reason": "Synthetic, benchmark, or replay cycle contamination",
        }

    # 2. Quarantined
    if doc.get("is_quarantined") is True:
        return {
            "id": dec_id,
            "cohort": "QUARANTINED",
            "disposition": "EXCLUDED",
            "learning_eligible": False,
            "reason": "Explicitly quarantined",
        }

    # 3. Explicitly excluded
    if doc.get("exclusion_reason") is not None:
        return {
            "id": dec_id,
            "cohort": "EXPLICITLY_EXCLUDED",
            "disposition": "EXCLUDED",
            "learning_eligible": False,
            "reason": f"Excluded: {doc.get('exclusion_reason')}",
        }

    # 4. Provenance unrecoverable
    if doc.get("outcome_evidence_state") == "provenance_unrecoverable":
        return {
            "id": dec_id,
            "cohort": "PROVENANCE_UNRECOVERABLE",
            "disposition": "EXCLUDED",
            "learning_eligible": False,
            "reason": "Missing reference bar or unresolvable price source",
        }

    # 5. Unsupported claim
    if doc.get("outcome_evidence_state") == "unsupported_claim":
        return {
            "id": dec_id,
            "cohort": "UNSUPPORTED_CLAIM",
            "disposition": "EXCLUDED",
            "learning_eligible": False,
            "reason": "Unverifiable trigger or conditional order without trigger path",
        }

    # 6. Pending resolution (v2, v3, or v4)
    evidence_state = doc.get("outcome_evidence_state")
    mat_status = doc.get("maturity_status")
    if evidence_state == "pending" or mat_status in ("PENDING", "WAITING_DAILY_BAR"):
        return {
            "id": dec_id,
            "cohort": "PENDING_RESOLUTION",
            "disposition": "PENDING",
            "learning_eligible": False,
            "reason": "Awaiting horizon maturity daily bar",
        }

    # 7. Contract v4 Mature Verified
    contract_ver = doc.get("contract_version")
    if contract_ver == 4 and mat_status == "MATURE_VERIFIED":
        return {
            "id": dec_id,
            "cohort": "CONTRACT_V4_MATURE_VERIFIED",
            "disposition": "QUALIFIED_LEARNING",
            "learning_eligible": True,
            "reason": "Contract v4 mature verified with full provenance",
        }

    # 8. Legacy Contract v2 / v3 Verified
    leg_contract_ver = doc.get("outcome_contract_version")
    if leg_contract_ver in (2, 3) and evidence_state == "verified":
        entry_src = doc.get("entry_price_source")
        exit_src = doc.get("exit_price_source")
        if entry_src and exit_src and entry_src == exit_src:
            return {
                "id": dec_id,
                "cohort": f"CONTRACT_V{leg_contract_ver}_VERIFIED",
                "disposition": "QUALIFIED_LEARNING",
                "learning_eligible": True,
                "reason": f"Legacy Contract v{leg_contract_ver} verified with matching vendor ({entry_src})",
            }
        return {
            "id": dec_id,
            "cohort": "LEGACY_VENDOR_MISMATCH",
            "disposition": "EXCLUDED",
            "learning_eligible": False,
            "reason": f"Mismatched price sources ({entry_src} != {exit_src})",
        }

    # 9. Legacy unversioned
    if contract_ver is None and leg_contract_ver is None:
        return {
            "id": dec_id,
            "cohort": "LEGACY_UNVERSIONED",
            "disposition": "HISTORICAL_ARCHIVE",
            "learning_eligible": False,
            "reason": "Legacy pre-contract record preserved as historical archive",
        }

    # Default fallback: fail-closed
    return {
        "id": dec_id,
        "cohort": "UNCLASSIFIED_FAIL_CLOSED",
        "disposition": "EXCLUDED",
        "learning_eligible": False,
        "reason": "Fails contract invariants",
    }


def classify_lot_closure(doc: dict[str, Any]) -> dict[str, Any]:
    """Classifies a position lot closure into a disposition cohort."""
    closure_id = doc.get("closure_id") or str(doc.get("_id", ""))

    origin = doc.get("origin")
    is_attributable = doc.get("is_attributable") is True
    prov_complete = doc.get("provenance_complete") is True
    has_alloc_fees = doc.get("allocated_entry_fee") is not None and doc.get("invested_capital_denominator") is not None

    if origin == "LIVE" and is_attributable and prov_complete and has_alloc_fees:
        return {
            "closure_id": closure_id,
            "cohort": "LIVE_ATTRIBUTABLE_V4",
            "disposition": "ATTRIBUTABLE",
            "attribution_eligible": True,
            "reason": "Live execution lot with strictly conserved fees and complete provenance",
        }

    if origin == "HISTORICAL_RECONSTRUCTION" and is_attributable and prov_complete and has_alloc_fees:
        return {
            "closure_id": closure_id,
            "cohort": "HISTORICAL_RECONSTRUCTION_ATTRIBUTABLE",
            "disposition": "ATTRIBUTABLE",
            "attribution_eligible": True,
            "reason": "Reconstructed lot with verified fills and allocated fees",
        }

    # Pre-Step 09 historical closure
    return {
        "closure_id": closure_id,
        "cohort": "LEGACY_UNALLOCATED_CLOSURE",
        "disposition": "HISTORICAL_ARCHIVE_EXCLUDED",
        "attribution_eligible": False,
        "reason": "Pre-v4 closure missing entry fee allocation and invested capital denominator",
    }


def audit_historical_database(db: Any) -> dict[str, Any]:
    """Performs a comprehensive, read-only audit across decision outcomes and lot closures."""
    dec_docs = list(db["decision_outcomes"].find({}))
    lot_docs = list(db["lot_closures"].find({}))
    pl_docs = list(db["position_lots"].find({}))

    # Classify decision outcomes
    dec_cohort_counts: dict[str, int] = {}
    dec_disposition_counts: dict[str, int] = {}
    dec_eligible_learning_count = 0

    for d in dec_docs:
        c = classify_decision_outcome(d)
        cohort = c["cohort"]
        disp = c["disposition"]
        dec_cohort_counts[cohort] = dec_cohort_counts.get(cohort, 0) + 1
        dec_disposition_counts[disp] = dec_disposition_counts.get(disp, 0) + 1
        if c["learning_eligible"]:
            dec_eligible_learning_count += 1
            # Invariant: must match outcome_reader logic
            assert _check_learning_eligibility(d) is True, f"Mismatch on doc {d.get('decision_id')}"
        else:
            assert _check_learning_eligibility(d) is False, f"Silent upgrade defect on doc {d.get('decision_id')}"

    # Verify reconciliation: sum of cohorts == total
    total_dec = len(dec_docs)
    assert sum(dec_cohort_counts.values()) == total_dec, "Decision outcome cohort counts do not reconcile!"

    # Classify lot closures
    lot_cohort_counts: dict[str, int] = {}
    lot_attributable_count = 0

    for l in lot_docs:
        c = classify_lot_closure(l)
        cohort = c["cohort"]
        lot_cohort_counts[cohort] = lot_cohort_counts.get(cohort, 0) + 1
        if c["attribution_eligible"]:
            lot_attributable_count += 1

    total_lots = len(lot_docs)
    assert sum(lot_cohort_counts.values()) == total_lots, "Lot closure cohort counts do not reconcile!"

    return {
        "decision_outcomes": {
            "total": total_dec,
            "cohort_counts": dec_cohort_counts,
            "disposition_counts": dec_disposition_counts,
            "learning_eligible_count": dec_eligible_learning_count,
            "reconciled": sum(dec_cohort_counts.values()) == total_dec,
            "no_silent_upgrades_verified": True,
        },
        "lot_closures": {
            "total": total_lots,
            "cohort_counts": lot_cohort_counts,
            "attributable_count": lot_attributable_count,
            "reconciled": sum(lot_cohort_counts.values()) == total_lots,
        },
        "position_lots": {
            "total": len(pl_docs),
            "open_count": sum(1 for p in pl_docs if p.get("status") == "open"),
            "closed_count": sum(1 for p in pl_docs if p.get("status") == "closed"),
        },
    }
