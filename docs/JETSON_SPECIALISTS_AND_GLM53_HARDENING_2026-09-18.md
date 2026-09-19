# Jetson Specialist Models & GLM-5.3 Pipeline Hardening Documentation

**Date:** 2026-09-18  
**Component:** `trading-service` (LazyCat420 owned) & `lazy-agent-service`  
**Author:** Antigravity (Pair Programming with LazyCat420)  
**Status:** Implemented, Validated with 149/149 Tests (56 Acceptance + 93 Unit), Merged to Master, and Deployed to Synology NAS

---

## 1. Overview & Resolved Production Blockers

This document records the resolution of all 8 production blockers from `trading-service 7f821c43` and the 12 user-directed architectural hardening specifications for the Jetson Orin Feature Platform (`gliner`, `market_cnn`, `timeseries_rnn`) and Gold Spark GLM-5.3 autonomous retraining loop:

1. **Production-Path Cycle Wiring (`app/v3/orchestrator.py`)**:
   - Wired `_build_specialist_features_task` to read real inputs from `precollect_stats["raw_data"]` (with non-overriding Mongo fallback).
   - Called real client inference endpoints (`extract_entities`, `classify_market_regime`, `predict_forecast`).
   - Pinned specialist model versions in `SharedDesk` (`desk.pinned_specialist_versions`) for cycle consistency.
   - Bound all specialist calls to a 5.0s shared deadline budget (`SPECIALIST_DEADLINE_SECONDS`).
   - Format: GLiNER documents include `document_id` and parse extracted entities from `result.documents`.

2. **Specialist Advisory Rendering & Cognition Receipts (`app/v3/shared_desk.py`, `app/v3/agent_runner.py`)**:
   - Added Fact Qualification Notice to GLiNER: extracted entities are clearly marked as candidate text mentions, not verified facts.
   - Grouped CNN and RNN under `Price-Derived Technical Signals (Shared OHLCV Source — Correlated Dimensions)` with an explicit Anti-Double-Counting Warning.
   - Moved delivery receipt logging to the exact moment of prompt assembly and dispatch in `run_v3_agent`.

3. **Temporal Target Isolation & Train-Only Normalization (`app/services/dataset_manifest_builder.py`)**:
   - Added `purge_hours` support creating temporal purge/embargo isolation between train, validation, and test splits.
   - Added `fit_and_apply_normalization`: z-score and min-max feature statistics are fit strictly on the train partition and applied out-of-sample to validation and test partitions.
   - Protect holdouts by content and provenance: canonical sample content hashing (`sample_content_hash`) prevents renamed or copied holdout files from entering training datasets.

4. **Atomic Retraining Budgets & Cooldowns (`app/services/glm_retraining_proposal_service.py`)**:
   - Transactional budget reservation with thread-safe `_budget_lock` and persistent storage.
   - Added `reconcile_failed_submission` to refund reserved capacity if job submission fails.
   - Added `check_holdout_collision` verifying dataset content hashes against protected holdouts.

5. **Durable Training Jobs & Multi-Worker Leases (`app/services/durable_training_service.py`)**:
   - Multi-worker atomic job admission with deterministic oldest-job tie-breaker under concurrency.
   - Strict conditional lease acquisition and heartbeat renewals.
   - Crash recovery resumes existing remote Jetson jobs without duplicate submissions.
   - Background worker loop (`start_worker_loop`) with graceful shutdown.

6. **Decay Monitoring & Automated Rollback (`app/services/decay_monitor_service.py`)**:
   - Automatic rollback triggered on calibration decay, coverage degradation, or precision collapse.
   - Post-rollback active model read-back verification (`RollbackVerificationError`).
   - Background worker loop (`start_worker_loop`) integrated into daemon lifecycle.

7. **Market Window Binding & Leakage Defense (`app/services/jetson_feature_client.py`)**:
   - Bound forecast calls to exact market window (`cutoff`, `window_end`).
   - Rejects future bars with timestamps beyond `cutoff` (`FeatureServiceResponseError` with `STALE_OR_MISMATCHED_OUTPUT`).
   - Enforces response hash and instrument ID verification.

8. **Server-Side Candidate Evaluation Before Promotion (`app/routers/feature_training_router.py`)**:
   - Eliminated caller-supplied metric bypass in `POST /models/{candidate_id}/promote`.
   - Evaluates candidate server-side on holdout slices, verifies against champion on identical manifest, and executes post-promotion active model verification.

---

## 2. Test Verification Matrix

### Acceptance Test Suite (56/56 Tests Passing)
- `tests/acceptance/test_acceptance_1_specialist_cycle.py` (4 tests) — Advisory delivery, receipt logging, fact qualification notice, anti-double-counting notice, paired sabotage test.
- `tests/acceptance/test_acceptance_2_training_http_routes.py` (3 tests) — Curate and train HTTP routes for GLiNER, CNN, RNN.
- `tests/acceptance/test_acceptance_3_mongo_durability_two_workers.py` (5 tests) — Real MongoDB concurrency admission, lease expiration, crash recovery, cursor semantics, paired lease theft sabotage test.
- `tests/acceptance/test_acceptance_4_promotion_attack_suite.py` (14 tests) — Rejection of missing sample counts, identity mismatch, champion regression, slice regression, optimistic lock failure, post-promotion verification failure.
- `tests/acceptance/test_acceptance_5_annotation_dataset_integrity.py` (9 tests) — GLM annotation status taxonomy, quarantine handling, duplicate resolution, holdout protection.
- `tests/acceptance/test_acceptance_6_proposal_and_limits.py` (7 tests) — GLM retraining proposal budget limits, cooldown enforcement, duplicate rejection, parameter validation.
- `tests/acceptance/test_acceptance_7_decay_and_verified_rollback.py` (7 tests) — Decay detection across all 3 models, incident deduplication, read-back verification failure detection, HTTP decay route.
- `tests/acceptance/test_acceptance_8_inference_routing_failures.py` (3 tests) — Capability verification gate, context capacity rejection, explicit UNAVAILABLE status without fabricated zeros.
- `tests/acceptance/test_acceptance_9_benchmarks.py` (2 tests) — Dynamic empirical latency distribution (p50/p90) and token metrics (>2.0x speedup, >50% token savings, latency caps), paired 500ms latency perturbation sabotage test.

### Unit Test Suite (93/93 Tests Passing)
- `tests/unit/test_dataset_manifest_builder.py` (2 tests)
- `tests/unit/test_dataset_manifest_integrity.py` (5 tests)
- `tests/unit/test_decay_monitor_and_rollback.py` (4 tests)
- `tests/unit/test_durable_training_service.py` (4 tests)
- `tests/unit/test_glm_proposal_bounds.py` (12 tests)
- `tests/unit/test_jetson_benchmark.py` (15 tests)
- `tests/unit/test_jetson_feature_client.py` (11 tests)
- `tests/unit/test_jetson_prompt_optimizations.py` (6 tests)
- `tests/unit/test_jetson_training_client.py` (7 tests)
- `tests/unit/test_schema_manifest_generation.py` (19 tests)
- `tests/unit/test_shared_desk_specialists.py` (8 tests)

---

## 3. Operational Deployment Status

- **Git Master Branch**: All work committed (`6d40adc5`), merged into `master`, and pushed to GitHub (`origin/master`).
- **Pre-Commit Secret Verification**: Zero static secrets, credentials, or high-entropy tokens staged.
- **NAS Deployment**: Deployed to Synology NAS container via `npm run deploy -- --skip-pull`.
- **Health Verification**:
  - Container: `trading-service` is `healthy` (Up 54s+).
  - Health Endpoint: `http://10.0.0.16:3031/health` returns `{"status":"ok","service":"trading-service","version":"v3"}`.
  - Background Loops: `DurableTrainingService` and `DecayMonitorService` actively polling Jetson health every 5.0s.
- **SSH Multiplexing**: `~/.ssh/sockets/lazycat@10.0.0.16:5188` active with `ControlMaster auto` and `ControlPersist 60m`.
