# Completion Receipt — Step 08: Fix Maturity Scheduling and Recovery

- **Step ID**: `08`
- **Title**: Fix maturity scheduling and recovery
- **Owner**: LazyCat420
- **Start Timestamp**: `2026-09-17T19:20:00Z`
- **End Timestamp**: `2026-09-17T19:32:00Z`
- **Status**: `COMPLETE`

---

## 1. Observed Baseline & Gap Identification

Prior to Step 08:
1. `run_mature_outcome_evaluation_iteration` in `app/trading/attribution/worker.py` queried candidate decisions using a hardcoded 7-day cutoff: `cutoff = now - timedelta(days=7); query = {"created_at": {"$lte": cutoff}}`. Decisions declared with 1-day horizons were forced to wait a full 7 days before being evaluated.
2. The candidate query sorted strictly by `created_at: 1` with `limit(50)`. If 50 older records had 30-day horizons or repeatedly failed evaluation, they occupied the entire top-50 candidate slice on every poll, completely starving newer valid 1-day or 7-day records behind them.
3. Malformed records in `decision_artifacts` threw unhandled validation exceptions that were logged with a warning, but the document in MongoDB remained untouched. On the very next poll, the exact same malformed document was returned at the top of the sort, creating permanent starvation of all subsequent work.
4. Retry backoff and bounded retry limits were absent: missing market data caused records to retry every hour forever without an upper bound or quarantine.
5. In crash scenarios where `decision_outcomes` was committed but the process crashed before marking `decision_artifacts`, restart recovery did not update `decision_artifacts`, risking redundant processing on every restart.
6. Execution checkpoints were not recorded to track evaluation progress.

---

## 2. Changes Made

1. **Attribution Model Lifecycles (`app/trading/attribution/models.py`)**:
   - Added lifecycle attributes to `DecisionArtifact`:
     - `maturity_date: Optional[datetime.datetime]` (automatically computed from `created_at + timedelta(days=declared_horizon_days or 7)` via `@model_validator(mode="after")`).
     - `retry_count: int = 0`
     - `is_quarantined: bool = False`
     - `quarantine_reason: Optional[str] = None`
     - `retry_after: Optional[datetime.datetime] = None`
     - `outcome_status: Optional[str] = None`
     - `evaluated_at: Optional[datetime.datetime] = None`
   - Added UTC timezone validators for all optional datetimes.

2. **Repository Indexes & Checkpoints (`app/trading/attribution/repository.py`)**:
   - Defined collections:
     - `COLL_DECISION_QUARANTINE = "decision_quarantine"`
     - `COLL_EVALUATION_CHECKPOINTS = "evaluation_checkpoints"`
     - `COLL_DECISION_OUTCOMES = "decision_outcomes"`
   - Updated `ensure_attribution_indexes()`:
     - Index on `decision_artifacts`: `[("is_quarantined", 1), ("outcome_status", 1), ("maturity_date", 1), ("retry_after", 1)]`.
     - Index on `decision_quarantine`: `[("decision_id", 1)]` and `[("quarantined_at", -1)]`.
     - Index on `evaluation_checkpoints`: `[("worker", 1)]` (unique).
   - Added helper functions:
     - `save_evaluation_checkpoint(worker_name, records_processed, last_evaluated_at)`
     - `get_evaluation_checkpoint(worker_name)`

3. **Maturity Scheduler & Starvation Prevention (`app/trading/attribution/worker.py`)**:
   - Refactored `run_mature_outcome_evaluation_iteration`:
     - Accepts optional `now` parameter for deterministic frozen-time testing.
     - Selects records where `is_quarantined != True`, `outcome_status not in (MATURE, EXCLUDED, QUARANTINED)`, `maturity_date <= eval_now` (or missing), and `retry_after <= eval_now` (or missing).
     - Sorts by `[("maturity_date", 1), ("created_at", 1)]` so earliest mature records are processed first regardless of submission age.
     - Self-healing legacy migration: automatically backfills `maturity_date` in MongoDB for legacy documents without it.
     - Immediate malformed document quarantine: validation failures immediately mark document `is_quarantined=True`, `outcome_status="QUARANTINED"`, and record details in `decision_quarantine`.
     - Persists evaluation checkpoints to `evaluation_checkpoints` upon batch completion.
   - Bounded retries:
     - Implemented `MAX_OUTCOME_RETRIES = 5` and `_record_evaluation_retry_or_exclusion`.
     - Unresolved records back off exponentially (`1h, 2h, 4h, 8h, 16h`).
     - Exceeding 5 retries marks the record `EXCLUDED`, sets `is_quarantined=True`, and writes to `decision_quarantine`.
   - Crash recovery & duplicate idempotency:
     - If `decision_outcomes` already contains `MATURE` / `MATURE_VERIFIED`, `evaluate_decision_at_horizon` updates `decision_artifacts` with `MATURE` and returns the existing outcome without re-evaluation or duplicate writes.

4. **Unit Test Suite (`tests/unit/test_maturity_scheduling.py`)**:
   - 7 unit tests covering:
     - Mixed 1d/7d/30d horizons scheduling.
     - Older ineligible records (>50) do not starve due 1-day records.
     - Malformed records quarantined without starving valid work.
     - Bounded exponential retries and terminal quarantine at MAX_OUTCOME_RETRIES (5).
     - Crash recovery and idempotent duplicate evaluation.
     - Frozen-time boundaries and evaluation checkpoints.
     - Legacy document dynamic `maturity_date` backfill.

5. **Real Mongo Integration Test Suite (`tests/integration/test_maturity_scheduling_mongo.py`)**:
   - 4 integration tests on `rs0` (`trading_bot_pytest`) verifying:
     - Mixed 1d/7d/30d horizons due-work scheduling with real price history.
     - Pagination over 65+ records across batches of 50 with restart recovery and checkpoints.
     - Malformed document quarantine on real MongoDB.
     - 55 older 30-day records do not starve due 1-day records.

---

## 3. Version Tracking

- **Source / Primary Commit**: `bfb721e6` (`master`)
- **Remote Head**: `origin/master` @ `bfb721e6`
- **Deployed Version**: `trading-service:bfb721e6` on Synology NAS (`10.0.0.16:3031`)
- **Live Deployed SHA Check**: Verified `deployed_commit_sha: "bfb721e6"` via `/control-plane/metrics`

---

## 4. Exact Validation Commands & Results

1. **Step 08 Unit Test Suite**:
   - Command: `/home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/unit/test_maturity_scheduling.py -v`
   - Results: **7 passed in 0.25s** (0 failed, 0 skipped).

2. **Step 08 Real Mongo Integration Test Suite**:
   - Command: `TRADING_BOT_MONGO_TEST=1 /home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/integration/test_maturity_scheduling_mongo.py -v`
   - Results: **4 passed in 32.82s** (0 failed, 0 skipped).

3. **Full Unit Regression Suite**:
   - Command: `/home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/unit/test_maturity_scheduling.py tests/unit/test_outcome_worker.py tests/unit/test_horizon_price_provenance.py tests/unit/test_outcome_contract.py tests/unit/test_control_plane_blockers_phase*.py tests/unit/test_attribution_models.py -v`
   - Results: **62 passed, 1 warning in 1.50s** (0 failed, 0 skipped).

4. **Full Real Mongo Integration Regression Suite**:
   - Command: `TRADING_BOT_MONGO_TEST=1 /home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/integration/test_maturity_scheduling_mongo.py tests/integration/test_horizon_price_provenance_mongo.py tests/integration/test_outcome_contract_mongo.py -v`
   - Results: **12 passed in 35.00s** (0 failed, 0 skipped).

5. **Live Deployment Verification**:
   - Transfer and restart: `master@bfb721e6` deployed via deploy-kit in 130s.
   - Health Endpoint: `curl -s http://10.0.0.16:3031/health` -> `{"status":"ok","service":"trading-service","version":"v3"}` (HTTP 200 OK).
   - Control-Plane Metrics: `curl -s http://10.0.0.16:3031/control-plane/metrics` -> `metrics_status: "HEALTHY"`, `deployed_commit_sha: "bfb721e6"`, `outbox_worker` and `outcome_worker` both `ALIVE`.

---

## 5. Exit Gate Checklist

- [x] Mixed 1/7/30-day records respect declared maturity timestamps.
- [x] More than 50 older ineligible records cannot starve due valid work.
- [x] Malformed records are quarantined into `decision_quarantine` and cannot block or starve valid work.
- [x] Bounded exponential retries (up to 5) prevent infinite retry loops; exceeding limit marks `EXCLUDED` and quarantines.
- [x] Crash recovery and duplicate evaluation are idempotent.
- [x] Frozen-time boundaries pass deterministically.
- [x] Disposable real-Mongo pagination (>50 records) and restart recovery pass within declared poll bounds.

---

## 6. Rollback & Invariant Verification

- Docker rollback image `trading-service:previous` preserved on Synology NAS.
- Reverting to commit `46191858` preserves database integrity as schema changes are additive.

---

## 7. Unresolved Issues

- None. Step 08 exit gate is fully satisfied.

---

## 8. Unlocked Next Step

- **Step 09**: Fix realized fee and lot accounting (`LOCKED` -> **READY**).
- Steps 10–30 remain **LOCKED**.
