# GLM 5.3 Dataset Curation & Jetson Training Loop Documentation

**Date:** 2026-09-18  
**Component:** `trading-service` (LazyCat420 owned) & Jetson Feature Platform (Port 8002)  
**Author:** Antigravity (Pair Programming with LazyCat420)  
**Status:** Implemented, Tested, and Verified Live

---

## 1. Overview & Objectives

Connected local LLM **GLM-5.3-Flash-EXL3** (served on Gold Spark via vLLM-2) to the **Jetson Feature Platform** (running on Jetson Orin on port 8002) to create a self-improving dataset curation, multi-sample consensus labeling, training orchestration, and deterministic promotion loop for non-LLM specialized models (`gliner`, `market_cnn`, `timeseries_rnn`).

---

## 2. Architectural Components

```
┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
│       Gold Spark (vLLM-2)            │        │        MongoDB (trading_bot)         │
│     GLM-5.3-Flash-EXL3 (500k ctx)    │        │  - news_articles                     │
│ http://10.0.0.16:5591/vllm-shim/...  │        │  - feature_lineage                   │
└──────────────────┬───────────────────┘        │  - training_dataset_manifests        │
                   │                            └──────────────────┬───────────────────┘
       Prompts & Candidate Annotations                             │ Ingests Unlabeled Data
                   │                                               │
┌──────────────────▼───────────────────────────────────────────────▼───────────────────┐
│ trading-service (Backend)                                                            │
│                                                                                      │
│ 1. GLMCuratorService (app/services/glm_curator_service.py)                           │
│    - Prompts GLM 5.3 for financial entity & corporate event extraction               │
│    - Executes multi-sample consensus voting (>=2/3 agreement) to kill hallucinations │
│    - Resolves character boundaries and converts to GLiNER tokenized NER format       │
│                                                                                      │
│ 2. DatasetManifestBuilder (app/services/dataset_manifest_builder.py)                 │
│    - Chronological 80/10/10 partitioning (train/val/frozen holdout test)             │
│    - Zero lookahead bias timestamp validation                                        │
│    - SHA-256 data integrity hashing                                                 │
│                                                                                      │
│ 3. JetsonTrainingOrchestrator (app/services/jetson_training_orchestrator.py)         │
│    - Pre-flight capacity check on Orin (training_active < training_max)              │
│    - Asynchronous training job submission & progress polling                         │
│    - Frozen test holdout evaluation trigger                                          │
│    - Deterministic policy gate (F1 > champion, Brier score, RMSE, latency cap)       │
│    - Promotes candidate or logs rejected reason                                      │
│                                                                                      │
│ 4. JetsonFeatureClient (app/services/jetson_feature_client.py)                        │
│    - submit_training_job(), list_training_jobs(), get_training_job()                 │
│    - cancel_training_job(), evaluate_candidate(), promote_candidate()                │
│    - rollback_model()                                                                │
│                                                                                      │
│ 5. FeatureTrainingRouter (app/routers/feature_training_router.py)                    │
│    - GET  /features/training/health                                                  │
│    - GET  /features/training/jobs                                                    │
│    - GET  /features/training/jobs/{job_id}                                           │
│    - POST /features/training/jobs/{job_id}/cancel                                    │
│    - POST /features/training/curate-and-train                                        │
│    - POST /features/training/models/{candidate_id}/evaluate                          │
│    - POST /features/training/models/{candidate_id}/promote                           │
│    - POST /features/training/models/{model_id}/rollback                              │
└──────────────────────────────────────┬───────────────────────────────────────────────┘
                                       │ Async Job Submission & Frozen Evaluation
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ Jetson Feature Platform (http://10.0.0.30:8002)                                      │
│ - Orin GPU (62.8 GB Unified VRAM)                                                    │
│ - Active Champions: gliner, market_cnn, timeseries_rnn                               │
│ - POST /v1/training/jobs (Background Subprocess Training, concurrency cap 1)         │
│ - POST /v1/models/{candidate_id}/evaluate (Frozen Benchmark Test Suite)              │
│ - POST /v1/models/{candidate_id}/promote (Policy Gate Promotion)                     │
│ - POST /v1/models/{model_id}/rollback (Prior Champion Restoration)                   │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Strict Guardrails Enforced

1. **GLM 5.3 is the Proposer, NOT the Gatekeeper:**
   - GLM 5.3 proposes annotations, labels, and candidate datasets.
   - GLM 5.3 does not have authority to promote its own proposed models.
   - Model promotion is determined strictly by deterministic mathematical evaluation metrics against frozen test suites.
2. **Multi-Sample Consensus Verification:**
   - Single-pass LLM extraction is prone to occasional hallucinations.
   - `GLMCuratorService` samples GLM 5.3 multiple times ($T=0.1, 0.2, 0.3$). Only entity spans identified in $\ge 2$ passes with matching span boundaries and identical canonical ticker mapping are accepted.
3. **Jetson Orin Single-Concurrency Gate:**
   - Jetson Orin has a hardware concurrency limit of 1 training job (`training_max: 1`).
   - `JetsonTrainingOrchestrator` verifies queue capacity prior to submission.
4. **Zero Lookahead Bias:**
   - Training, validation, and holdout datasets are partitioned strictly chronologically ($T_{\text{train}} \le T_{\text{val}} \le T_{\text{test}}$).

---

## 4. Test Verification Summary

All 28 tests passing across unit and live integration suites:
- `tests/unit/test_jetson_feature_client.py` (10 passed)
- `tests/unit/test_jetson_training_client.py` (7 passed)
- `tests/unit/test_glm_curator.py` (4 passed)
- `tests/unit/test_dataset_manifest_builder.py` (2 passed)
- `tests/unit/test_training_orchestrator.py` (3 passed)
- `tests/unit/test_feature_training_router.py` (3 passed)
- `tests/integration/test_live_glm_jetson_training_loop.py` (2 passed against live Gold Spark GLM 5.3 and live Jetson port 8002)
