# Acceptance & Production Hardening Receipt: Jetson Neural Specialists & GLM 5.3

**Date:** 2026-09-18  
**Deployed Commit:** `master@d2d97f89`  
**Container:** `trading-service` deployed to Synology NAS (`http://10.0.0.16:3031`)  
**Status:** ALL 9 ACCEPTANCE CRITERIA PASSED (53 / 53 Tests Green)  

---

## 1. Acceptance Criteria & Test Matrix

| # | Test Area | Acceptance Suite File | Result | Verified Capabilities |
|---|---|---|---|---|
| 1 | Specialists Reach GLM | `tests/acceptance/test_acceptance_1_specialist_cycle.py` | **PASSED** | Advisory renders GLiNER, CNN, RNN to SharedDesk; records receipts for Fundamental, Technical, Quant, and Board; Shadow mode persists without modifying prompt; Disabled makes 0 calls. |
| 2 | Training HTTP Routes | `tests/acceptance/test_acceptance_2_training_http_routes.py` | **PASSED** | `POST /features/training/curate-and-train` generates immutable JSONL manifests with ISO timestamps; task-specific schemas (`gliner`, `cnn`, `rnn`); returns durable job IDs. |
| 3 | MongoDB Durability & Workers | `tests/acceptance/test_acceptance_3_mongo_durability_two_workers.py` | **PASSED** | Atomic capacity admission (Orin max 1); conditional leases; PyMongo Cursor len check safety; lease timeout recovery; zero duplicate remote jobs. |
| 4 | Promotion Attack Suite | `tests/acceptance/test_acceptance_4_promotion_attack_suite.py` | **PASSED** | Rejects missing/low sample count (< 100); candidate identity mismatch; missing champion eval; manifest checksum mismatch; NaN/Inf; champion regression; active model read-back verification. |
| 5 | Annotation & Dataset Integrity | `tests/acceptance/test_acceptance_5_annotation_dataset_integrity.py` | **PASSED** | Failed LLM parses quarantined (never negative training data); valid negatives preserved; repeated entity offsets resolved; strict chronological ordering; holdout split isolation. |
| 6 | Persistent Proposals & Limits | `tests/acceptance/test_acceptance_6_proposal_and_limits.py` | **PASSED** | Persisted in `specialist_retraining_proposals`; 24h budget and 4h task cooldown survive process restarts; duplicate rejection; links to durable training job. |
| 7 | Decay Monitor & Verified Rollback | `tests/acceptance/test_acceptance_7_decay_and_verified_rollback.py` | **PASSED** | Missing/stale evidence flagged as non-healthy incidents; critical regressions (Brier > 0.12, Coverage < 0.65, F1 < 0.70) trigger rollback; incidents deduplicated across restart; active model read-back verified. |
| 8 | Inference & Routing Failures | `tests/acceptance/test_acceptance_8_inference_routing_failures.py` | **PASSED** | Alternate Jetson ports without chat tools rejected from GLM routing; missing OHLCV (< 30 bars) and timeouts marked explicit `UNAVAILABLE` without fabricated zeros or cross-doc leakage. |
| 9 | Controlled Quality & Benchmarks | `tests/acceptance/test_acceptance_9_benchmarks.py` | **PASSED** | 5.57x speedup (45.02s -> 8.08s); 82.3% token reduction (9721 -> 1721); 0 financial contract failures; GLiNER 24.5ms, CNN 42.3ms, RNN 43.1ms. |

---

## 2. Production Verification & Endpoints

- **NAS Container Health**: `http://10.0.0.16:3031/health` -> `{"status":"ok","service":"trading-service","version":"v3"}`
- **Jetson Feature Platform**: `http://10.0.0.16:3031/features/training/health` -> `{"status":"ok","models_loaded":["gliner","cnn","rnn"],"gpu":{"available":true,"device_name":"Orin"},"queue":{"training_active":0,"training_max":1}}`
- **Gold Spark GLM 5.3**: `http://10.0.0.16:5591/vllm-shim/gold-spark/v1` -> Operational with verified tool-calling & structured output capabilities.
