# Completion Receipt — Step 07: Fix Horizon-Price Provenance

- **Step ID**: `07`
- **Title**: Fix horizon-price provenance
- **Owner**: LazyCat420
- **Start Timestamp**: `2026-09-17T19:12:00Z`
- **End Timestamp**: `2026-09-17T19:20:00Z`
- **Status**: `COMPLETE`

---

## 1. Observed Baseline & Gap Identification

Prior to Step 07:
1. `app/trading/attribution/worker.py` queried historical prices using only `["close", "price"]` without capturing data vendor, source identity, completed daily bar dates, adjustment conventions, or vendor hashes.
2. In both `_get_benchmark_price` and `_get_asset_historical_price`, a fallback to `_get_current_price` was executed whenever `target_dt` was within 24 hours of now. This allowed a missing historical horizon bar to be silently substituted with a live intraday quote, violating historical provenance, introducing lookahead, and corrupting reproducibility.
3. No calendar-aware age tolerance was enforced in the attribution worker; an arbitrarily old quote could be selected if no recent bars existed.
4. Source pinning was absent between entry and horizon observations: an entry quote from one vendor (e.g. `alpaca`) could be evaluated against a horizon bar from another vendor (e.g. `polygon` or `tiingo`), injecting artificial price jumps due to differing adjustment conventions (e.g. raw vs dividend/split-adjusted).
5. Unadjusted corporate actions and stock splits were not checked at evaluation time, allowing unadjusted split drops (e.g. 4:1 or 10:1) to masquerade as severe trading losses.

---

## 2. Changes Made

1. **New Provenance Module (`app/trading/attribution/provenance.py`)**:
   - Implemented `get_source_pinned_observation()`:
     - Enforces calendar-aware age tolerance (5 days for US equities to handle 2-day weekends and 3/4-day holiday weekends; 2 days for 24/7 crypto).
     - Resolves completed daily close bar availability cutoffs (16:15 US/Eastern for equities; 00:00 UTC for crypto) to prevent future leakage.
     - Strictly forbids fallback to live/current quotes. Missing historical horizon bars return `None`.
     - Enforces source pinning: if `pinned_source` is supplied, queries strictly require `source == pinned_source`.
     - Generates deterministic sha256 vendor hash (`source:ticker:date:close:adjustment`).
     - Detects unadjusted corporate actions and stock splits, marking `adjustment_convention = AdjustmentConvention.UNADJUSTED`.

2. **Attribution Worker Pricing Overhaul (`app/trading/attribution/worker.py`)**:
   - Stripped `_get_current_price` fallback completely from historical horizon pricing in `_get_benchmark_price` and `_get_asset_historical_price`.
   - Updated `evaluate_decision_at_horizon`:
     - Obtains entry source from reference quote or source-pinned entry bar.
     - Queries horizon observation strictly matching `pinned_source`.
     - Missing horizon bar marks outcome `UNRESOLVED` with reason `STALE_HORIZON_BAR` and schedules bounded retry.
     - Unadjusted split marks outcome `EXCLUDED` / `CONTAMINATED` with reason `CORPORATE_ACTION_UNADJUSTED`.
     - Missing benchmark observation marks outcome `UNRESOLVED` with reason `MISSING_BENCHMARK_BAR`.
     - Persists unified v4 contract attributes alongside backward-compatible v3 fields.
     - Reconstructs existing records with clean validation.
   - Updated `evaluate_closed_lot_alpha_iteration` to dynamically resolve benchmarks via `BenchmarkSpec.resolve(ticker)` instead of hardcoding `SPY`.

3. **Attribution Model Evolution (`app/trading/attribution/models.py`)**:
   - Added `model_config = ConfigDict(extra="ignore", populate_by_name=True)` to legacy `DecisionOutcomeRecord` to permit smooth backward-compatible reading of v4 additive fields.

4. **Unit Test Suite (`tests/unit/test_horizon_price_provenance.py`)**:
   - 8 unit tests covering:
     - Weekend & holiday calendar age tolerance.
     - Old-only history beyond tolerance returning `None`.
     - Missing horizon bar with valid current quote present MUST NOT resolve.
     - Unadjusted split detection and `CORPORATE_ACTION_UNADJUSTED` exclusion.
     - Mixed sources rejection.
     - Missing benchmark producing reasoned `UNRESOLVED`.
     - Delayed processing and replay parity with matching vendor hashes.
     - Repeated evaluation idempotency.

5. **Real Mongo Integration Suite (`tests/integration/test_horizon_price_provenance_mongo.py`)**:
   - 4 integration tests on `rs0` verifying real `price_history` queries, weekend cutoffs, live quote substitution prevention, multi-vendor pinning, and replay idempotency.

---

## 3. Version Tracking

- **Source / Primary Commit**: `46191858` (`master`)
- **Remote Head**: `origin/master` @ `46191858`
- **Deployed Version**: `trading-service:46191858` on Synology NAS (`10.0.0.16:3031`)
- **Live Deployed SHA Check**: Verified `deployed_commit_sha: "46191858"` via `/control-plane/metrics`

---

## 4. Exact Validation Commands & Results

1. **Step 07 Unit Test Suite**:
   - Command: `/home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/unit/test_horizon_price_provenance.py -v`
   - Results: **8 passed in 0.25s** (0 failed, 0 skipped).

2. **Step 07 Real Mongo Integration Test Suite**:
   - Command: `TRADING_BOT_MONGO_TEST=1 /home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/integration/test_horizon_price_provenance_mongo.py -v`
   - Results: **4 passed in 13.77s** (0 failed, 0 skipped).

3. **Full Unit Regression Suite**:
   - Command: `/home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/unit/test_horizon_price_provenance.py tests/unit/test_outcome_contract.py tests/unit/test_control_plane_blockers_phase*.py tests/unit/test_attribution_models.py -v`
   - Results: **52 passed, 1 warning in 1.47s** (0 failed, 0 skipped).

4. **Full Real Mongo Integration Regression Suite**:
   - Command: `TRADING_BOT_MONGO_TEST=1 /home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/integration/test_horizon_price_provenance_mongo.py tests/integration/test_outcome_contract_mongo.py -v`
   - Results: **8 passed in 14.17s** (0 failed, 0 skipped).

5. **Live Deployment Verification**:
   - Transfer and restart: `master@46191858` deployed via deploy-kit in 147s.
   - Health Endpoint: `curl -s http://10.0.0.16:3031/health` -> `{"status":"ok","service":"trading-service","version":"v3"}` (HTTP 200 OK).
   - Control-Plane Metrics: `curl -s http://10.0.0.16:3031/control-plane/metrics` -> `metrics_status: "HEALTHY"`, `deployed_commit_sha: "46191858"`, `outbox_worker` and `outcome_worker` both `ALIVE`.

---

## 5. Exit Gate Checklist

- [x] Tests cover weekends/holidays within calendar tolerance.
- [x] Tests cover unadjusted splits (flagged `CORPORATE_ACTION_UNADJUSTED`).
- [x] Tests cover old-only history (returns None, evaluated as UNRESOLVED).
- [x] Tests cover missing asset/benchmark (evaluated as reasoned UNRESOLVED).
- [x] Tests cover mixed sources (entry source pinned, mismatched source rejected).
- [x] Tests cover delayed processing and replay (identical observations and hashes).
- [x] A missing horizon bar plus a valid current quote MUST NOT resolve (verified).
- [x] Repeated evaluation uses the same versioned observations (idempotent).

---

## 6. Rollback & Invariant Verification

- Docker rollback image `trading-service:previous` preserved on Synology NAS.
- Reverting to commit `76dcc42b` preserves database integrity as schema changes are additive.

---

## 7. Unresolved Issues

None. All Step 07 requirements and exit gates passed with 100% compliance.

---

## 8. Next Unlocked Step

- **Step 08 — Fix maturity scheduling and recovery**: READY (UNLOCKED).
- **Steps 09–30**: LOCKED.
