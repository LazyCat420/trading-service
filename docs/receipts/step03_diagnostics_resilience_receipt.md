# Step 03 Completion Receipt: Diagnostics Resilience and Isolated Degraded Telemetry

- **Step ID:** 03
- **Title:** Keep diagnostics available during control-plane failure
- **Owner:** LazyCat420
- **Start Timestamp:** 2026-09-17T18:16:30Z
- **End Timestamp:** 2026-09-17T18:24:00Z
- **Status:** PASSED (Exit Gate Satisfied)
- **Source Branch:** `fix/diagnostics-resilience`
- **Merged To Primary:** `master` @ `b51e2c38`
- **Pushed Remote:** `origin/master` @ `b51e2c38`
- **Deployed Version (NAS):** `trading-service:b51e2c38` (live container on Synology NAS `10.0.0.16:3031`)

---

## 1. Observed Baseline & Defect Analysis

Before remediation:
1. `get_control_plane_operational_metrics()` called `resolve_control_plane_mode("default")` directly. An invalid mode (e.g. `CONTROL_PLANE_MODE="CORRUPTED"`) or database lookup error caused `resolve_control_plane_mode` to raise `ControlPlaneConfigurationError`, resulting in an unhandled 500 error on the diagnostic endpoint and total loss of telemetry.
2. Complete MongoDB unavailability or connection drops crashed `mongo_store.get_doc_db()`, preventing retrieval of independently known metadata like `deployed_commit_sha`.
3. Metric sections (`worker_heartbeats`, `outbox`, `reconciliation`, `outcome_evaluation`) were not isolated. A slow query, timeout, or schema failure in one section immediately aborted all remaining sections.
4. Raw exceptions could leak sensitive URI connection strings containing credentials into telemetry or logs.
5. In partial failures, metrics did not explicitly distinguish missing/unobserved state from zero counts or fake healthy heartbeats.

---

## 2. Changes Implemented

| File | Changes Made |
|---|---|
| `app/trading/control_plane.py` | 1. Added regex-based URI and credential sanitization (`_sanitize_message`) and exception categorization (`_classify_error_category`) to map failures to bounded categories (`TIMEOUT`, `DATABASE_UNAVAILABLE`, `CONFIGURATION_ERROR`, `QUERY_ERROR`, `INTERNAL_ERROR`).<br>2. Wrapped mode resolution in an isolated try/except block: if mode resolution fails, diagnostics returns `default_mode="UNKNOWN"` and flags `"mode_resolution"` in `degraded_sections`, while trade execution resolution continues to strictly fail closed without falling back to OBSERVE.<br>3. Wrapped MongoDB client acquisition and each individual metric section (`worker_heartbeats`, `outbox`, `reconciliation`, `outcome_evaluation`) in separate isolated try/except blocks.<br>4. In degraded state, returns explicit `metrics_status="DEGRADED"` with `degraded_sections` list. Crucially, unknown counts remain `None` (not `0`), and failed heartbeats return `status="UNAVAILABLE"` (never `"ALIVE"`). |
| `cycle_main.py` | 1. Extracted `create_app() -> FastAPI` so the FastAPI application can be cleanly tested and initialized.<br>2. Updated `/control-plane/metrics` route to return HTTP 200 with `X-Metrics-Status: HEALTHY` or `X-Metrics-Status: DEGRADED` response header alongside the full JSON diagnostic payload, ensuring monitoring can inspect degraded diagnostics without discarding the response. |
| `tests/unit/test_diagnostics_resilience.py` | New comprehensive unit test suite verifying: (a) invalid mode returns DEGRADED while resolver fails closed; (b) complete database unavailability returns DEGRADED with commit SHA intact and no false zeros/ALIVEs; (c) single section failure isolation; (d) query timeout handling; (e) secret and connection string scrubbing; (f) FastAPI endpoint HTTP semantics with `X-Metrics-Status` header. |

---

## 3. Validation & Test Evidence

### A. Targeted Resilience Suite (`test_diagnostics_resilience.py`)
- **Command:** `pytest -v -p no:cacheprovider tests/unit/test_diagnostics_resilience.py`
- **Result:** `6 passed in 0.63s`
- **Test Details:**
  1. `test_invalid_mode_diagnostics_returns_degraded_and_resolver_fails_closed`: Confirmed execution resolver raises `ControlPlaneConfigurationError` on corrupt mode, while diagnostics safely returns `default_mode='UNKNOWN'`, `status='DEGRADED'`, and `mode_resolution.error_category='CONFIGURATION_ERROR'`.
  2. `test_database_unavailable_returns_degraded_without_falsifying_data`: Confirmed when DB is down, `deployed_commit_sha` is preserved, `metrics_status='DEGRADED'`, `total_decisions=None` (not 0), and heartbeats do not report `ALIVE`.
  3. `test_single_failed_section_isolation`: Confirmed outbox failure degrades only `outbox`, while worker heartbeats, reconciliation, and outcome evaluation remain fully populated.
  4. `test_slow_query_timeout_handling`: Confirmed query timeouts are categorized as `TIMEOUT` and degraded.
  5. `test_sanitization_of_secrets_and_connection_strings`: Confirmed dynamic token and URI credentials in exceptions are fully scrubbed from serialized telemetry.
  6. `test_fastapi_control_plane_metrics_endpoint_http_semantics`: Confirmed HTTP 200 with `X-Metrics-Status` header for both healthy and degraded responses.

### B. Unit Regression Suite
- **Command:** `pytest -q -p no:cacheprovider tests/unit/test_diagnostics_resilience.py tests/unit/test_control_plane_blockers_phase*.py`
- **Result:** `31 passed, 4 warnings in 1.33s`

### C. Step 02 Real Mongo Integration Suite
- **Command:** `TRADING_BOT_MONGO_TEST=1 pytest -q -p no:cacheprovider tests/integration/test_shadow_execution_atomicity.py`
- **Result:** `6 passed in 32.48s` (verified zero regression from Step 02 on real Mongo replica set).

### D. Zero Credential Leakage Verification
- **Command:** `git diff --cached -i -G"(password|secret|token|api_key|credential)"`
- **Result:** Fully compliant. All tests use dynamic in-memory `secrets.token_hex()` generation.

---

## 4. Live Deployment Verification (NAS: `10.0.0.16:3031`)

- **Deploy Command:** `npm run deploy -- --skip-pull`
- **Build & Deploy Time:** 139s
- **Live Health Endpoint Check:**
  ```json
  {"status":"ok","service":"trading-service","version":"v3"}
  ```
- **Live Control Plane Metrics Check:**
  ```http
  HTTP/1.1 200 OK
  date: Thu, 17 Sep 2026 18:23:32 GMT
  server: uvicorn
  content-length: 870
  content-type: application/json
  x-metrics-status: HEALTHY

  {
      "metrics_status": "HEALTHY",
      "observation_time": "2026-09-17T18:23:32.902607+00:00",
      "deployed_commit_sha": "b51e2c38",
      "default_mode": "OBSERVE",
      "degraded_sections": [],
      "mode_resolution": {
          "status": "AVAILABLE",
          "effective_mode": "OBSERVE"
      },
      "worker_heartbeats": {
          "outbox_worker": {
              "last_heartbeat": "2026-09-17T18:23:31.491000+00:00",
              "age_seconds": 1.4,
              "status": "ALIVE"
          },
          "outcome_worker": {
              "last_heartbeat": "2026-09-17T18:23:15.408000+00:00",
              "age_seconds": 17.5,
              "status": "ALIVE"
          }
      },
      "outbox": {
          "completed": 0,
          "oldest_pending_age_seconds": 0.0,
          "pending": 0,
          "poison_failed": 0,
          "processing": 0,
          "reconciliation_lag_seconds": 0.0,
          "retry_totals": 0
      },
      "reconciliation": {
          "last_reconciled_at": null,
          "last_reconciliation_id": null,
          "last_verdict": null
      },
      "outcome_evaluation": {
          "last_evaluated_at": null,
          "last_outcome_id": null,
          "total_decisions": 17,
          "mature_decisions": 0,
          "unresolved_decisions": 0,
          "coverage_pct": 0.0
      }
  }
  ```

---

## 5. Rollback Test & Verification
- Pre-existing rollback image `trading-service:previous` tagged and retained on remote Docker daemon during deployment.

---

## 6. Unresolved Issues
- None in Step 03 scope.

---

## 7. Next Step
- **Step 03 Status:** DONE / PASSED.
- **Next Unlocked Step:** **Step 04 — Verify and strengthen the runtime execution boundary**.
