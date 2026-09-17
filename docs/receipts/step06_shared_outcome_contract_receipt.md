# Completion Receipt — Step 06: Freeze the Shared Outcome Contract

- **Step ID**: `06`
- **Title**: Freeze the shared outcome contract
- **Owner**: LazyCat420
- **Start Timestamp**: `2026-09-17T19:04:00Z`
- **End Timestamp**: `2026-09-17T19:12:00Z`
- **Status**: `COMPLETE`

---

## 1. Observed Baseline & Gap Identification

Prior to Step 06:
1. Outcomes were loosely typed across different subsystems: `app/autoresearch/outcome_evidence.py` had Contract v3 for `immediate_directional` and `flat_wait`, while `app/trading/attribution/models.py` had a flat `DecisionOutcomeRecord`.
2. Liquidations (`SELL` to close a long position) were not formally separated from initiating directional short forecasts (`SHORT_PREDICTION`).
3. Holding an existing position (`HOLD` with `hold_reason_held=True`) was not cleanly separated from sitting in cash (`FLAT_WAIT` with `hold_reason_held=False`).
4. Missing execution evidence could inadvertently be conflated with thesis failure; there was no typed guarantee that missing execution evidence cannot produce realized net alpha.
5. Fee accounting risked double-deducting friction when fills already incorporated broker spread and slippage.
6. Benchmark symbol was universally hardcoded to `SPY` in multiple places rather than dynamically resolving by instrument and asset class (e.g., `BTC` for crypto assets).
7. Report slicing lacked explicit structures for tracking transparent counts across `not_yet_due`, `due_unresolved`, `evaluated_mature`, and `excluded` (with reason breakdown).

---

## 2. Changes Made

1. **New Canonical Contract Module (`app/trading/attribution/outcome_contract.py`)**:
   - Implemented the 7 distinct outcome claim formulations:
     - `PROPOSAL_DIRECTION`: Thesis directional evaluation for BUY or prospective short forecast.
     - `EXIT_TIMING`: Exit timing / liquidation of an existing position.
     - `FLAT_WAIT`: HOLD decision when flat (cash preservation / avoided decline).
     - `HELD_POSITION`: HOLD decision when holding inventory (position maintenance).
     - `CONDITIONAL_ENTRY`: Limit / stop trigger path evaluations.
     - `POLICY_COUNTERFACTUAL`: Counterfactual outcome if policy had not intervened/capped.
     - `REALIZED_LOT`: Realized closed tax-lot economic cash-flow accounting.
   - Built action classifier `distinguish_action()`:
     - Distinguishes `close-long SELL` (when position > 0) from `short prediction` (when flat).
     - Distinguishes `held HOLD` (when position > 0) from `flat wait` (when flat).
     - Distinguishes `immediate BUY` from `conditional BUY`.
   - Invariant: Defined `forecast_alpha` (`decision_return - benchmark_return`) and `realized_net_alpha` (`executed_return - benchmark_return`). Guaranteed that missing execution fills leave `realized_net_alpha = None` and `executed_return = None`.
   - Cash Flow Accounting (`calculate_net_cashflow_return`):
     - Conserves cash flows and prevents fee double-counting when spread/slippage is already embedded in fill prices.
     - Net return = `(Cash Inflow - Cash Outflow) / Cash Outflow * 100`.
   - Dynamic Benchmark Resolution (`BenchmarkSpec.resolve()`):
     - Dynamically resolves crypto tickers to `BTC` and equity instruments to `SPY`.
   - Market Calendar Cutoffs (`HorizonSpec.closed_bar_cutoff()`):
     - 16:15 US/Eastern completed daily bar cutoff for US equities; 00:00 UTC cutoff for 24/7 crypto.
   - Strict Learning Eligibility Predicate (`is_eligible_for_learning()`):
     - Restricts learning strictly to `MATURE_VERIFIED` records with source-pinned matched price observations, excluding synthetic cycles, delisted/suspended symbols, and unadjusted corporate actions.
   - Report Slicing Specification (`OutcomeReportSlice`):
     - Exposes transparent counts for `not_yet_due`, `due_unresolved`, `evaluated_mature`, and `excluded` (with reason breakdown).
   - Logical-to-Physical Collections Mapping (`LOGICAL_TO_PHYSICAL_COLLECTIONS`):
     - Formally maps logical stores to physical MongoDB collections (`decision_outcomes`, `lot_closures`, `decision_artifacts`, `policy_decisions`, `execution_intents`, `attribution_reports`, etc.).

2. **Attribution Models Integration (`app/trading/attribution/models.py`)**:
   - Re-exported all v4 contract classes and helpers.
   - Added `to_v4()` adapter method on legacy `DecisionOutcomeRecord`, providing seamless backward compatibility for existing v3 readers while migrating to v4.

3. **Unit Test Suite (`tests/unit/test_outcome_contract.py`)**:
   - 13 comprehensive unit tests validating all exit gate requirements.

4. **Integration Test Suite (`tests/integration/test_outcome_contract_mongo.py`)**:
   - Real MongoDB replica set verification for v4 persistence, lot closure tracking, v3/v4 coexistence in `decision_outcomes`, and aggregation slicing.

---

## 3. Version Tracking

- **Source / Primary Commit**: `76dcc42b` (`master`)
- **Remote Head**: `origin/master` @ `76dcc42b`
- **Deployed Version**: `trading-service:76dcc42b` on Synology NAS (`10.0.0.16:3031`)
- **Live Deployed SHA Check**: Verified `deployed_commit_sha: "76dcc42b"` via `/control-plane/metrics`

---

## 4. Exact Validation Commands & Results

1. **Unit Test Suite (`test_outcome_contract.py`)**:
   - Command: `/home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/unit/test_outcome_contract.py -v`
   - Results: **13 passed in 0.31s** (0 failed, 0 skipped).

2. **Integration Test Suite (`test_outcome_contract_mongo.py`)**:
   - Command: `TRADING_BOT_MONGO_TEST=1 /home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/integration/test_outcome_contract_mongo.py -v`
   - Results: **4 passed in 1.71s** (0 failed, 0 skipped).

3. **Unit Regression Suite**:
   - Command: `/home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/unit/test_outcome_contract.py tests/unit/test_attribution_models.py tests/unit/test_control_plane_blockers_phase*.py -v`
   - Results: **44 passed in 1.36s** (0 failed, 0 skipped).

4. **Live Deployment Verification**:
   - Transfer and restart: `master@76dcc42b` deployed via deploy-kit in 130s.
   - Health Endpoint: `curl -s http://10.0.0.16:3031/health` -> `{"status":"ok","service":"trading-service","version":"v3"}` (HTTP 200 OK).
   - Control-Plane Metrics: `curl -s http://10.0.0.16:3031/control-plane/metrics` -> `metrics_status: "HEALTHY"`, `deployed_commit_sha: "76dcc42b"`, `outbox_worker` and `outcome_worker` both `ALIVE`.

---

## 5. Exit Gate Checklist

- [x] Contract fixtures distinguish `close-long SELL` from `short prediction`.
- [x] Contract fixtures distinguish `held HOLD` from `flat wait`.
- [x] Missing execution evidence CANNOT become realized alpha (invariant verified).
- [x] Cash flow returns conserve fees and prevent double deduction.
- [x] Producers/readers and logical-to-physical collections are mapped (`LOGICAL_TO_PHYSICAL_COLLECTIONS`).
- [x] The versioning plan preserves existing readers (`to_v4()` adapter and v3/v4 co-existence verified).

---

## 6. Rollback & Invariant Verification

- Docker rollback tag `trading-service:previous` preserved on Synology NAS.
- Reverting to previous commit `cc4a2cd5` leaves existing v3 database records intact because v4 changes are purely additive.

---

## 7. Unresolved Issues

None. All Step 06 requirements and exit gates passed with zero warnings or errors.

---

## 8. Next Unlocked Step

- **Step 07 — Fix horizon-price provenance**: READY (UNLOCKED).
- **Steps 08–30**: LOCKED.
