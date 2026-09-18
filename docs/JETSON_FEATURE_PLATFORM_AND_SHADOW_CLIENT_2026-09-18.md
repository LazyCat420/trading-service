# Jetson Feature Platform & Trading-Cycle Feature Client

**Date:** 2026-09-18  
**Author:** LazyCat420  
**Status:** Implemented & Verified (Shadow Mode Active)  
**Target Repositories:** `trading-service`  

---

## 1. Overview & Architectural Decoupling

Specialized machine learning models (`gliner`, `market_cnn`, `timeseries_rnn`) are non-conversational, specialized feature extractors. As established in [Chapter 129](file:///home/lazycat/github/projects/sun/trading-client/documentation/chapters/129-jetson-bonsai-model-and-portfolio-cycle-cleanup-2026-09-18.md), these models are excluded from LLM discovery lists and chat endpoints (`/v1/chat/completions`).

To provide low-latency, source-attributable facts and calibrated mathematical priors without bloating conversational context, we established two independently testable workstreams:
1. **Jetson Feature Platform** (`http://10.0.0.30:8002`): Dedicated FastAPI microservice on the Jetson running non-LLM specialized extractors.
2. **Trading-Cycle Feature Client** (`app/services/jetson_feature_client.py`): An isolated, circuit-breaker-protected async client in `trading-service` communicating directly with port 8002, persisting immutable feature lineage into MongoDB (`trading_bot.feature_lineage`), and operating in **shadow mode** for initial evaluation.

---

## 2. API Contract (Port 8002)

### 2.1 Operational & Model Routes
- `GET /health`: Liveness, readiness, GPU availability, and queue status.
- `GET /v1/capabilities`: Supported tasks, models, schemas, and concurrency limits.
- `GET /v1/models`: Active champions, candidates, and artifact versions.

### 2.2 Feature Inference Endpoints
- `POST /v1/features/entities`: GLiNER NER & event extraction over text documents.
- `POST /v1/features/market-regime`: CNN market-regime classification over normalized 128-bar OHLCV tensors.
- `POST /v1/features/forecast`: RNN probabilistic return quantile forecast over historical lookback windows.

### 2.3 Training & Promotion Lifecycle
- `POST /v1/training/jobs`: Asynchronous submission of candidate training jobs.
- `GET /v1/training/jobs/{job_id}`: Training progress, loss curves, and artifact hashes.
- `POST /v1/models/{candidate_id}/evaluate`: Frozen holdout suite evaluation.
- `POST /v1/models/{candidate_id}/promote`: Policy-gated promotion to champion.
- `POST /v1/models/{model_id}/rollback`: Instant rollback to prior champion.

---

## 3. Implementation Details

### 3.1 Client Architecture (`app/services/jetson_feature_client.py`)
- **Transport**: `httpx.AsyncClient` with configurable timeout (`JETSON_FEATURE_TIMEOUT_SECONDS=5.0`) and bounded retries (`JETSON_FEATURE_MAX_RETRIES=2`).
- **Circuit Breaker**: Trips to `OPEN` after 3 consecutive connection or server failures, avoiding hanging trading cycle tasks. Probes in `HALF_OPEN` after a 60s cooldown.
- **Envelope Validation**: Verifies `request_id`, `model_id`, `model_version`, `schema_version`, `input_hash`, and `result`.
- **Typed Errors**: Automatically parses and raises `FeatureServiceResponseError` with upstream error codes (`INVALID_REQUEST`, `UNSUPPORTED_SCHEMA`, `MODEL_UNAVAILABLE`, `GPU_BUSY`, `TIMEOUT`).

### 3.2 MongoDB Feature Lineage (`app/services/feature_lineage_store.py`)
Persists all feature inferences to `trading_bot.feature_lineage`:
- Indexes: `(cycle_id, instrument_id)`, `(model_id, created_at)`, `(input_hash)`, `(document_id)`.
- Metadata: `cycle_id`, `decision_id`, `instrument_id`, `document_id`, `market_window_id`, `model_id`, `model_version`, `schema_version`, `input_hash`, `source_data_timestamp`, `request_id`, `latency_ms`, `mode` (`shadow` vs `advisory`), and `payload`.

### 3.3 Shadow Mode News Ingestion (`app/services/news_extraction.py`)
- When `ensure_facts` receives news rows, it asynchronously triggers `_dispatch_gliner_shadow(todo)`.
- Runs via `asyncio.create_task` without blocking the cycle wall budget.
- Downstream LLM facts returned to the agents remain 100% unaltered, satisfying strict Phase 1 shadow isolation.

---

## 4. Verification Suite

1. **Unit Tests** (`tests/unit/test_jetson_feature_client.py`):
   - Input hashing: SHA-256 deterministic computation.
   - Capability caching: TTL verification without redundant network traffic.
   - Circuit breaker: Tripping on consecutive failures, fast-failing in `OPEN`, and auto-resetting in `HALF_OPEN`.
   - Error handling: 4xx non-retry vs 5xx retry with backoff.
   - Contracts: GLiNER, CNN, and RNN payload contracts verified.
   - Persistence: Mongo document insertion and query isolation.

2. **Integration Tests** (`tests/integration/test_gliner_shadow_pipeline.py`):
   - End-to-end shadow mode dispatch during news extraction.
   - Verification that downstream facts map is untouched by GLiNER.
   - Verification that offline/failing feature service fails open without impacting cycle execution.

3. **Execution Results**:
   ```
   tests/unit/test_jetson_feature_client.py: 10 passed
   tests/integration/test_gliner_shadow_pipeline.py: 2 passed
   ============================== 12 passed in 1.69s ==============================
   ```
