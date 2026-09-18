# Jetson Specialist Models & GLM-5.3 Pipeline Hardening Documentation

**Date:** 2026-09-18  
**Component:** `trading-service` (LazyCat420 owned) & `lazy-agent-service`  
**Author:** Antigravity (Pair Programming with LazyCat420)  
**Status:** Implemented, Validated with 58/58 Tests, Merged to Master, and Deployed to Synology NAS

---

## 1. Overview & Resolved Auditor Specifications

This document tracks the resolution of all 13 specifications identified in the system audit for the Jetson Orin Feature Platform (`gliner`, `market_cnn`, `timeseries_rnn`) and Gold Spark GLM-5.3 integration:

1. **Cycle Wiring & Lineage Tracking**: Added `disabled`, `shadow`, and `advisory` modes to `SharedDesk`. Typed neural intelligence is rendered into the compressed context only during `advisory` mode. Feature lineage is tracked per deciding agent in `agent_telemetry`.
2. **Fail-Closed Promotion Gates**: `evaluate_promotion_gate` rejects missing metrics, `NaN`/`inf`, non-numeric types, unknown tasks, and insufficient sample sizes ($N < 100$ for GLiNER/CNN, $N < 500$ for RNN). Added post-promotion active model verification (`PromotionVerificationError`).
3. **Champion Anti-Regression**: Evaluates candidate and champion on the same versioned datasets; requires non-regression on critical slices ($> 2\%$ regression rejected); binds promotion to evaluated champion version via optimistic locking.
4. **Dataset Chronology & Delivery**: Strict ISO timestamp parsing (zero epoch 0 fallback); deduplication of articles via content hashing; temporal embargo gaps between splits; independent `train_sha256`, `val_sha256`, and `test_sha256` digests stored in durable manifests.
5. **Annotation Status Taxonomy**: Separated `VALID_NEGATIVE` from `FAILED` (syntax/transport error) and `UNCERTAIN_QUARANTINE`. Added multi-occurrence span resolution (`resolve_all_spans_in_text`) and strict ontology enum validation (`ALLOWED_LABELS`).
6. **Task-Specific Training Formats**: Built dedicated builders for GLiNER tokenized spans, Market CNN $30\times 8$ normalized tensors, and Timeseries RNN $25$-step sequences with mature return quantiles.
7. **Bounded GLM Autonomous Retraining**: `GLMRetrainingProposalService` enforces learning rate $\in [5\times 10^{-6}, 5\times 10^{-4}]$, batch size $\in [8, 32]$, epochs $\in [1, 5]$, maximum 2 jobs per 24 hours, 4-hour task cooldowns, and proposal deduplication. Protected holdout datasets and promotion thresholds remain strictly isolated.
8. **Durable Training Jobs**: MongoDB collection `training_jobs` manages the state machine `SUBMITTED → QUEUED → ADMITTED → RUNNING → EVALUATING → PROMOTED | REJECTED | FAILED | TIMEOUT` with atomic admission (`training_active < training_max`), 60s leases, heartbeats, and stale lease crash recovery.
9. **Model Routing Hardening**: Removed invented 256k context limits in `lazy-agent-service`; hardened port fallback to require `altResponse.ok` (never activates on 404s). Added auto-healing in `VllmShimService` when upstreams report a missing model.
10. **Feature Contract Enforcement**: OHLCV validation rejects $< 30$ bars, zero-padding, and non-positive/non-finite prices. Validates monotonic return quantiles ($p_{10} \le p_{50} \le p_{90}$) and chunks GLiNER documents into batches $\le 45$.
11. **Production-Path Paired Benchmarks**: Replaced benchmark shortcuts with `scripts/benchmarks/cycle_specialist_ab_eval.py` executing live `SharedDesk` workflows across standard tickers, measuring latency, token usage, failures, calibration, and grounding.
12. **Decay Monitoring & Automated Rollback**: Implemented `DecayMonitorService` rolling out-of-time evaluation circuit breaker triggering automated rollback when:
    - Market CNN Brier score $> 0.12$
    - Timeseries RNN 80% interval coverage $< 0.65$
    - GLiNER extraction F1 $< 0.70$
13. **Code Consolidation**: Shared transport, normalization, schemas, and metric validators consolidated under `app/specialists/common/`.

---

## 2. Test Verification Matrix

All 10 test modules pass 100% in local and CI environments:
- `tests/unit/test_fail_closed_promotion.py` (11 tests)
- `tests/unit/test_training_orchestrator.py` (3 tests)
- `tests/unit/test_dataset_manifest_integrity.py` (4 tests)
- `tests/unit/test_dataset_manifest_builder.py` (2 tests)
- `tests/unit/test_glm_consensus_curation.py` (6 tests)
- `tests/unit/test_glm_curator.py` (4 tests)
- `tests/unit/test_durable_training_service.py` (4 tests)
- `tests/unit/test_shared_desk_specialists.py` (8 tests)
- `tests/unit/test_glm_proposal_bounds.py` (12 tests)
- `tests/unit/test_decay_monitor_and_rollback.py` (4 tests)

---

## 3. Operational Deployment Status

- **`lazy-agent-service`**: Container rebuilt and active on Synology NAS (`http://10.0.0.16:5591/health`).
- **`trading-service`**: Container rebuilt and active on Synology NAS (`http://10.0.0.16:3031/health`).
- **SSH Multiplexing**: `~/.ssh/sockets/lazycat@10.0.0.16:5188` active with `ControlPersist 60m`.
