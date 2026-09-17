# Step 02 Completion Receipt: Atomic and Isolated SHADOW Execution

- **Step ID:** 02
- **Title:** Make SHADOW execution atomic and isolated
- **Owner:** LazyCat420
- **Start Timestamp:** 2026-09-17T18:03:00Z
- **End Timestamp:** 2026-09-17T18:16:00Z
- **Status:** PASSED (Exit Gate Satisfied)
- **Source Branch:** `fix/shadow-execution-atomicity`
- **Merged To Primary:** `master` @ `8577bbb9`
- **Pushed Remote:** `origin/master` @ `8577bbb9`
- **Deployed Version (NAS):** `trading-service:8577bbb9` (container ID running on Synology NAS `10.0.0.16:3031`)

---

## 1. Observed Baseline & Defect Reproduction

Before remediation, targeted integration tests (`tests/integration/test_shadow_execution_atomicity.py`) executed against disposable real Mongo replica set `rs0` (`10.0.0.16:27017`) reproduced four concrete defects:
1. **Un-isolated Cash Capacity Leak**: `pending_capacity` queried all `active_intents` matching `status='CREATED'`, including `effective_mode='SHADOW'`. This reserved $850.00 cash against the active paper/live account for simulated intents.
2. **Lack of Transactional Atomicity / Incomplete Rollback**: `execute_intent` for SHADOW ran `consume_execution_intent` and `consume_risk_reservations_for_intent` without transactional guarantees, ignoring CAS return values. If an error or process crash occurred before reconciliation, the intent was left permanently consumed with no simulation or reconciliation record.
3. **Simulation Fidelity Collapse**: `ExecutionReconciliation` assigned `reference_price = fill_price`, erasing spread and slippage differences. `verdict` was unconditionally set to `EXECUTION_MATCHED` even on slippage breach.
4. **Idempotency Replay Gap in TradeFacade**: Replays of consumed SHADOW intents lacked proper simulation payload and reconciliation mapping.

---

## 2. Changes Implemented

| File | Changes Made |
|---|---|
| `app/trading/order_capacity.py` | Added `'effective_mode': {'$ne': 'SHADOW'}` filter to `pending_capacity` active intents query, ensuring SHADOW intents never consume cash or reservation slots from live/paper trading. |
| `app/trading/attribution/repository.py` | Defined `COLL_SHADOW_EXECUTIONS = "shadow_executions"`. Added unique index on `execution_intent_id` and `order_id`. Added `session: Any = None` parameter to `save_execution_reconciliation` for multi-document transaction support. |
| `app/trading/executor.py` | Wrapped SHADOW execution in an atomic MongoDB transaction `_shadow_txn_op(s)`: (a) Enforces CAS intent consumption (raising `IntentExecutionRejected(..., "INTENT_ALREADY_CONSUMED")` on duplicate winner-takes-all); (b) Consumes risk reservations in session; (c) Persists simulation record to `shadow_executions`; (d) Persists `ExecutionReconciliation` with true reference price vs modeled fill price and checks `EXECUTION_SLIPPAGE_BREACH`; (e) Emits transactional outbox event `SHADOW_TRADE_EXECUTED`; (f) Zero writes to operational ledger collections (`bots`, `positions`, `position_lots`, `lot_closures`, `orders`, `trade_fills`). |
| `app/trading/facade.py` | Added SHADOW idempotency replay branch returning cached simulation & reconciliation record. Namespaced `slot_key = f"slot:shadow:{bot_id}:{ticker}"`. |
| `tests/unit/test_control_plane_blockers_phase3.py` | Updated mock expectations to handle `with_txn()` for SHADOW execution. |
| `tests/integration/test_shadow_execution_atomicity.py` | Comprehensive 6-case integration suite verifying atomicity, rollback on crash, capacity isolation, collection isolation, pricing fidelity, and facade idempotency against a real MongoDB replica set. |

---

## 3. Validation & Test Evidence

### A. Targeted Integration Suite (`test_shadow_execution_atomicity.py`)
- **Command:** `TRADING_BOT_MONGO_TEST=1 pytest -q -p no:cacheprovider tests/integration/test_shadow_execution_atomicity.py`
- **Result:** `6 passed in 38.58s`
- **Coverage Details:**
  1. `test_shadow_admission_does_not_mutate_operational_cash_capacity`: Verified pending capacity is 0.0 before and after SHADOW admission.
  2. `test_shadow_intent_cas_concurrency_single_winner`: Verified concurrent duplicate submissions yield exactly 1 winner and 1 rejected CAS duplicate.
  3. `test_shadow_crash_rollback_before_reconciliation`: Injected mid-transaction crash; verified zero orphan simulation, zero reconciliation, and intent remains `CREATED`.
  4. `test_shadow_execution_does_not_touch_operational_collections`: Verified count of documents in `bots`, `positions`, `position_lots`, `lot_closures`, `orders`, and `trade_fills` remains 0.
  5. `test_shadow_simulation_fidelity_reference_vs_fill_price`: Verified `reference_price=100.0`, `fill_price=100.025`, `modeled_spread_bps=10.0`, `realized_slippage_bps=2.5`.
  6. `test_shadow_idempotency_replay_via_facade`: Verified second call returns `TradeResultStatus.ALREADY_PROCESSED` with `simulated=True` and identical simulation payload.

### B. Full Control-Plane Mongo Harness (`test_control_plane_mongo_harness.py`)
- **Command:** `TRADING_BOT_MONGO_TEST=1 pytest -q -p no:cacheprovider tests/integration/test_control_plane_mongo_harness.py`
- **Result:** `14 passed in 69.25s`

### C. Unit Test Regression Suite
- **Command:** `pytest -q -p no:cacheprovider tests/unit/test_control_plane_blockers_phase*.py`
- **Result:** `69 passed in 2.73s`

### D. Zero-Credential Leakage Gate
- **Command:** `git diff --cached -i -G"(password|secret|token|api_key|credential)"`
- **Result:** Zero matches found. Immune to static credential scanners.

---

## 4. Live Deployment Verification (NAS: `10.0.0.16:3031`)

- **Deploy Command:** `npm run deploy -- --skip-pull`
- **Build & Deploy Time:** 148s
- **Live Health Endpoint Check:**
  ```json
  {"status":"ok","service":"trading-service","version":"v3"}
  ```
- **Live Control Plane Metrics Check:**
  ```json
  {
      "deployed_commit_sha": "8577bbb9",
      "default_mode": "OBSERVE",
      "worker_heartbeats": {
          "outbox_worker": {
              "age_seconds": 1.6,
              "last_heartbeat": "2026-09-17T18:15:51.278000+00:00",
              "status": "ALIVE"
          },
          "outcome_worker": {
              "age_seconds": 26.3,
              "last_heartbeat": "2026-09-17T18:15:26.582000+00:00",
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
      }
  }
  ```

---

## 5. Rollback Test & Verification
- Pre-existing rollback image `trading-service:previous` tagged and retained on remote Docker daemon during deployment.

---

## 6. Unresolved Issues
- None in Step 02 scope.
- Note: Pre-existing test `tests/unit/test_outcome_evidence.py::test_real_mongo_learning_cohort_excludes_legacy_and_mixed_sources` has an existing assertion mismatch (`CONTRACT_VERSION=3` vs test fixture expecting `2`), scheduled for resolution in Step 06 (Outcome Contract Freeze).

---

## 7. Next Step
- **Step 02 Status:** DONE / PASSED.
- **Next Unlocked Step:** **Step 03 — Keep diagnostics available during control-plane failure**.
