# Historical Data Disposition Audit Report — Step 11

- **Audit Date**: `2026-09-17`
- **Scope**: All historical decision outcomes (`decision_outcomes`), lot closures (`lot_closures`), and position tax lots (`position_lots`) on production MongoDB.
- **Authority**: *Sequential Developer Plan Revision 3 — Step 11: Resolve historical-data treatment* under the Universal Evidence-Driven Problem-Solving Blueprint.
- **Operating Disposition**: Read-Only Classification & Clean-Break Quarantine. Zero production mutations.

---

## 1. Executive Summary

This audit establishes the explicit data disposition and clean-break classification across all historical trade and research records prior to the adoption of Contract v4 and the Unified Outcome Access Layer.

### Key Audit Guarantees
1. **Zero Silent Upgrades**: No unversioned, legacy, unsupported, or incomplete historical record has been silently upgraded to Contract v4 or marked as `MATURE_VERIFIED`.
2. **100% Cohort Reconciliation**: The sum of all categorized cohorts strictly equals total collection counts across all collections.
3. **Fail-Closed Attribution**: 100% of pre-Step 09 lot closures (31/31) are excluded from live alpha attribution due to missing entry fee allocation and invested capital denominators.
4. **Learning Eligibility Conservation**: Exactly 28 legacy Contract v2 records qualify for model training, all of which have verified matching price vendors. All 2,833 other historical outcomes are strictly quarantined or pending.

---

## 2. Decision Outcomes Census & Cohort Classification

- **Collection**: `decision_outcomes`
- **Total Records**: `2,861`
- **Time Range**: `2026-05-06T14:32:05Z` to `2026-09-17T15:33:50Z`

| Cohort | Count | Pct of Total | Disposition | Exclusion / Retention Reason |
|---|---|---|---|---|
| `LEGACY_UNVERSIONED` | 2,610 | 91.23% | `HISTORICAL_ARCHIVE` | Pre-contract legacy records. Preserved as execution history; excluded from learning cohorts. |
| `SYNTHETIC_CYCLE` | 97 | 3.39% | `EXCLUDED` | Synthetic test runs, replay fixtures, and benchmark cycles (`cycle-test-*`, `bench-*`, `sim-*`). Excluded by `exclude_synthetic()`. |
| `UNSUPPORTED_CLAIM` | 74 | 2.59% | `EXCLUDED` | Conditional orders, unheld HOLDs without trigger paths, and ungradeable claims. |
| `PENDING_RESOLUTION` | 49 | 1.71% | `PENDING` | Real market decisions under Contract v2/v3 awaiting 7-day daily bar closure. |
| `CONTRACT_V2_VERIFIED` | 28 | 0.98% | `QUALIFIED_LEARNING` | Contract v2 verified records with verified price pairs from identical vendor sources. |
| `PROVENANCE_UNRECOVERABLE` | 3 | 0.10% | `EXCLUDED` | Missing reference bars, vendor gaps, or corrupted price history. |
| **Total** | **2,861** | **100.00%** | — | **Reconciled (sum of cohorts == collection count)** |

---

## 3. Realized Lot Closures Census & Fee Conservation Audit

- **Collection**: `lot_closures`
- **Total Records**: `31`
- **Time Range**: `2026-06-18T06:22:52Z` to `2026-09-17T06:58:51Z`

| Cohort | Count | Pct of Total | Disposition | Attribution Eligibility |
|---|---|---|---|---|
| `LEGACY_UNALLOCATED_CLOSURE` | 31 | 100.00% | `HISTORICAL_ARCHIVE_EXCLUDED` | **Ineligible**: Lacks `allocated_entry_fee` and invested capital denominator. Excluded from live alpha attribution. |
| `LIVE_ATTRIBUTABLE_V4` | 0 | 0.00% | `ATTRIBUTABLE` | Active under Step 09 forward pipeline. |
| **Total** | **31** | **100.00%** | — | **Reconciled** |

---

## 4. Position Lots Inventory

- **Collection**: `position_lots`
- **Total Records**: `67`
- **Open Lots**: `36`
- **Closed Lots**: `31`
- **Disposition**: Pre-Step 09 tax lots lack `origin: "LIVE"` and `remaining_entry_fee`. They remain open for broker balance reconciliation but are flagged with historical provenance. New executions strictly require `origin: "LIVE"`, `entry_fee`, and `remaining_entry_fee`.

---

## 5. Old-Format Reader Compatibility & Exclusion Paths

Every historical reader was verified for fail-closed compatibility:
1. `app/trading/attribution/outcome_reader.py`:
   - `get_verified_decision_outcomes`: Filters out `is_quarantined: True`, `exclusion_reason != None`, synthetic cycles.
   - `get_learning_cohort_outcomes`: Restricts to Contract v4 `MATURE_VERIFIED` or legacy verified with matching price sources. Rejects all 2,610 unversioned and 174 excluded/quarantined rows.
   - `get_verified_closed_lot_outcomes`: Requires `is_attributable: True` and `provenance_complete: True`. All 31 legacy closures return 0 records for live alpha.
2. `scripts/agent_scorecard.py`:
   - Reads through `get_verified_decision_outcomes`, preventing corrupted or test runs from entering agent scores.
3. `app/autoresearch/eval_engine.py`:
   - Reads through `get_learning_cohort_outcomes`, preventing unversioned or mismatched rows from skewing calibration.
4. `scripts/decision_score_report.py`:
   - Joins on verified outcomes only, ensuring shadow comparison reports reflect verified returns.

---

## 6. Exit Gate Verdict

- [x] **Data-disposition audit complete and documented.**
- [x] **Cohort counts strictly reconcile down to the single document.**
- [x] **Zero unsupported rows silently upgraded.**
- [x] **Old-format readers maintain tested compatibility and exclusion paths.**
- [x] **Clean break active and verified.**
