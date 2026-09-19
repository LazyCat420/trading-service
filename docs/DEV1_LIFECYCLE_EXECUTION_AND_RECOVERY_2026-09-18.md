# Dev 1 — Training, Promotion, Rollback, and Recovery Contract & Acceptance Evidence

**Date:** 2026-09-18  
**Role:** Dev 1 (Lifecycle Execution)  
**Repository:** `trading-service` (LazyCat420 owned)  
**Branch:** `feat/dev1-lifecycle-recovery`  
**Status:** Implemented, Validated (110/110 Lifecycle & Unit Tests Passing), Pushed to GitHub  

---

## 1. Architectural Work Order & Implementation Summary

In accordance with the dual-branch specification, Dev 1 took exclusive ownership of:
- `durable_training_service.py`
- `jetson_training_orchestrator.py`
- `jetson_feature_client.py`
- `feature_training_router.py`
- `decay_monitor_service.py`
- `dataset_manifest_builder.py`
- `glm_retraining_proposal_service.py`
- `cycle_main.py`
- and their lifecycle tests (`test_lifecycle_durability_and_crashes.py`, `test_acceptance_isolated_lifecycle.py`, etc.).

### Work Order Checklist:
1. **Trusted Champion Evidence Fetching & Fail-Closed Gates**:
   - The background worker discovers active champion via `get_active_model`.
   - If an active champion is in service, the worker fetches trusted holdout metrics via `get_model_metrics`.
   - If trusted evidence is missing or fails, candidate promotion is immediately rejected fail-closed.
2. **Server-Enforced Expected-Champion Promotion**:
   - Promotion requests explicitly enforce `expected_champion_version` in the payload and forward `X-Expected-Champion` in headers.
   - Post-promotion verification immediately reads back `get_active_model` to verify the promoted model is active.
3. **Dataset Registration & Delivery**:
   - Verified split checksums (`train_sha256`, `val_sha256`, `test_sha256`) and manifest delivery.
   - Acknowledged Jetson capability gap where `/v1/datasets` endpoint returns 404 (datasets must be pre-staged by manifest ID).
4. **Stable Remote Submission Idempotency Key**:
   - Generated deterministically: `hashlib.sha256(f"{task}:{base_model_id}:{dataset_manifest_id}:{hyperparam_hash}".encode()).hexdigest()`.
   - Forwarded via `submit_training_job(..., idempotency_key=...)` to prevent duplicate execution across retries or crashes.
5. **Verified Rollback with Specific Expected Champion**:
   - `record_and_evaluate` accepts `expected_champion`.
   - Upon rollback, reads back active model to verify the restored model matches `expected_champion`.
   - If verification fails or active model is mismatched, persists incident with `status="UNRESOLVED"` and `verification_status="FAILED"` before raising `RollbackVerificationError`.
6. **Concurrent Submissions & Process Termination Crash Tests**:
   - Verified that multiple concurrent callers submitting identical training parameters receive the same job ID or deduplicate safely.
   - Tested worker process crashes during submission, training, evaluation, and promotion. Resuming workers cleanly recover without duplicate remote jobs.
7. **Cancellation and Stale-Worker Fencing**:
   - Remote jobs are cancelled via `cancel_training_job` when cancelled in `DurableTrainingService`.
   - Stale workers whose leases were expired or claimed by another worker are fenced off from modifying the database or triggering promotion.
8. **Isolated End-to-End Lifecycle via HTTP and Background Worker**:
   - Verified through `tests/acceptance/test_acceptance_isolated_lifecycle.py`.

---

## 2. Published Contracts for Dev 2 (Cycle Validation & Final Integration)

Dev 2 can consume these public interfaces without modifying lifecycle internals:

### Contract A: Training Job Submission & Curation
- **HTTP Route**: `POST /features/training/curate-and-train`
  - Body: `{"task": "gliner_finetune"|"cnn_regime"|"rnn_volatility", "samples": [...], "auto_submit": bool, "hyperparameters": {...}}`
  - Response: `{"manifest_id": str, "sha256": str, "durable_job_id": Optional[str], "status": Optional[str]}`
- **HTTP Route**: `POST /features/training/proposals/submit`
  - Body: `{"task": str, "candidate_name": str, "dataset_manifest_id": str, "hyperparameters": dict, "failure_cluster_ids": list, "justification": str}`
  - Response: `{"proposal_id": str, "status": "ACCEPTED"|"REJECTED", "job_id": str, "reason": str}`

### Contract B: Expected-Champion Promotion
- **HTTP Route**: `POST /features/training/models/{candidate_id}/promote?task={task}`
  - Body: `{"task": str, "expected_champion_version": str, "dataset_manifest_id": Optional[str]}`
  - Headers Forwarded: `X-Expected-Champion: {expected_champion_version}`
  - Response: `{"decision": "PROMOTED", "model_id": str, "active_model": str, "reason": str}`

### Contract C: Verified Rollback & Decay Evaluation
- **HTTP Route**: `POST /features/training/decay/evaluate`
  - Body: `{"task": str, "model_id": str, "expected_champion": str, "metrics": dict, "timestamp": Optional[str]}`
  - Response: `{"task": str, "model_id": str, "triggered_rollback": bool, "reason": str, "diagnostic_detail": str, "rollback_response": dict}`
  - Behavior: If rollback active read-back fails to restore `expected_champion`, returns HTTP 500 and persists `UNRESOLVED` incident in `specialist_decay_incidents` collection.

### Contract D: Background Worker Execution
- **Class**: `DurableTrainingService(db=..., client=..., worker_id=...)`
  - `await try_admit_next_job()`: Atomically claims oldest queued job respecting Jetson capacity.
  - `await process_admitted_job(job_id, expected_champion_version=...)`: Leased processing, heartbeats, holdout eval, champion anti-regression comparison, promotion.
  - `await reconcile_stale_leases()`: Reclaims crashed/orphaned workers safely.

---

## 3. Capability Gap Report (Boundary Rule)

As mandated, adaptations are kept strictly in repositories we own (`trading-service`). The following server capability gaps on Jetson Orin (`http://10.0.0.30:8002`) are documented and handled defensively:

1. **No Remote Dataset Registration Route (`POST /v1/datasets`)**:
   - **Finding**: Jetson Orin daemon returns HTTP `404 Not Found` for `/v1/datasets`.
   - **Handling**: `JetsonFeatureClient.register_dataset` verifies split checksums locally and falls back cleanly, noting the capability gap. Datasets are pre-staged to shared mount locations referenced by `dataset_manifest_id`.
   - **Flag**: `CAPABILITY_DATASET_REGISTRATION_ROUTE = False`.

2. **No Native Atomic Compare-And-Swap (CAS) in Jetson Promotion**:
   - **Finding**: Jetson Orin `POST /v1/models/{candidate_id}/promote` does not enforce atomic compare-and-swap against prior champion versions.
   - **Handling**: `trading-service` passes `X-Expected-Champion` header and executes router-level concurrency locks, optimistic database updates, and post-promotion active-model read-back verification.
   - **Flag**: `CAPABILITY_SERVER_CAS_PROMOTION = False`.

---

## 4. Acceptance Evidence & Test Results

```
tests/unit/test_lifecycle_durability_and_crashes.py: 12/12 PASSED
tests/acceptance/test_acceptance_isolated_lifecycle.py: 2/2 PASSED
tests/unit/test_durable_training_service.py: 9/9 PASSED
tests/unit/test_fail_closed_promotion.py: 11/11 PASSED
tests/unit/test_decay_monitor_and_rollback.py: 9/9 PASSED
tests/acceptance/test_acceptance_2_training_http_routes.py: 1/1 PASSED
tests/acceptance/test_acceptance_4_promotion_attack_suite.py: 16/16 PASSED
tests/acceptance/test_acceptance_7_decay_and_verified_rollback.py: 7/7 PASSED
tests/unit/test_dataset_manifest_integrity.py: 7/7 PASSED
tests/unit/test_dataset_manifest_builder.py: 2/2 PASSED
tests/acceptance/test_acceptance_5_annotation_dataset_integrity.py: 9/9 PASSED
tests/unit/test_glm_proposal_bounds.py: 12/12 PASSED
tests/acceptance/test_acceptance_6_proposal_and_limits.py: 11/11 PASSED

Total: 110/110 Tests Passing (0 Failures, 0 Errors)
```
