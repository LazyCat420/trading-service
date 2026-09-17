# Completion Receipt — Step 05: Prove Real Concurrency, Crashes and Migration Behavior

- **Step ID**: `05`
- **Title**: Prove real concurrency, crashes and migration behavior
- **Owner**: LazyCat420
- **Start Timestamp**: `2026-09-17T18:38:00Z`
- **End Timestamp**: `2026-09-17T19:04:00Z`
- **Status**: `COMPLETE`

---

## 1. Observed Baseline & Gap Identification

Prior to Step 05:
1. Concurrency testing in `test_scenario_3_concurrent_cash_reservation_race` was sequential: it saved one intent and checked pending capacity sequentially, rather than executing overlapping operations against a real MongoDB replica set.
2. Concurrent submissions sharing an `idempotency_key` could trigger uncaught `pymongo.errors.OperationFailure` (code 251 `NoSuchTransaction` / code 112 `WriteConflict`) inside `TradeFacade.submit_trade` when WiredTiger aborted a losing concurrent transaction.
3. In `app/trading/attribution/repository.py`, slot collision handling attempted `slot_col.find_one({"slot_key": slot_key}, session=s)` inside a transaction session that was already marked aborted by MongoDB after a duplicate key / write conflict.
4. Process crash and exception injection behavior across distinct write boundaries (order attempt, trade fill, position tax lot, transactional outbox) needed explicit atomic rollback proof on `rs0`.
5. Multi-lot FIFO partial closure, fee conservation, symbol normalization, and migration idempotency had not been verified together under concurrent execution against real replica set infrastructure.

---

## 2. Changes Made

1. **New Integration Suite (`tests/integration/test_real_concurrency_and_invariants.py`)**:
   - Implemented all 9 required test scenarios against the real MongoDB replica set `rs0` (`trading_bot_pytest`):
     - **Scenario 1**: Two BUY admissions competing for last cash ($10k balance, two $7k requests synchronized by `threading.Barrier(2)` in parallel threads). Exactly one succeeds; total reservations ($7k) never exceed capacity; cash balance is preserved; execution decrements cash and consumes reservation.
     - **Scenario 2**: Two concurrent submissions sharing an idempotency key (synchronized by barrier). Exactly one creates orders/fills; the loser receives `ALREADY_PROCESSED` without false success or duplicate execution.
     - **Scenario 3**: Expiry racing execution. Synchronized race proves clean serialization: either execution commits and consumes reservation, or expiry transitions to `EXPIRED` and execution fails closed. Zero stranded active reservations and zero duplicate spend.
     - **Scenario 4**: Outbox lease reclamation race. Worker A's lease expires; Worker B reclaims it. Worker A's attempt to complete returns `False` (0 documents modified). Worker B completes event.
     - **Scenario 5**: Failure injection at 4 critical write boundaries (`order_attempt`, `trade_fills`, `position_lots`, `execution_outbox`). Atomic rollback leaves intent in `CREATED` status, 0 orders, 0 fills, 0 lots, 0 outbox events, and cash balance untouched.
     - **Scenario 6**: Partial sells across multiple BUY lots (10 @ $100, 10 @ $110, selling 15 @ $120). Proves FIFO lot closure (Lot 1 closed, Lot 2 partial with 5 shares remaining), symbol normalization (`"  aapl  "` -> `"AAPL"`), and closure attribution tracking in `lot_closures`.
     - **Scenario 7**: Historical position-to-lot migration idempotency and ENFORCE promotion gating (`reconcile_and_migrate_bot_positions`). First run creates missing lots with `origin="MIGRATION"` and `provenance_complete=False`. Second run is completely idempotent (`lots_created=0`). Tampered lot quantity blocks promotion (`ValueError`); corrected lot permits promotion to `ENFORCE`.
     - **Scenario 8**: SHADOW same-key races and failure-boundary behavior. CAS single winner or write-conflict rollback ensures exactly 1 simulation document committed. Crash injection before commit leaves zero simulations.
     - **Scenario 9**: Representative failure mutation test. Disabling the cash reservation guard allows $16k reservations on a $10k account (demonstrating invariant breach). Restoring the guard immediately blocks over-reservation with `InsufficientCashReservationError`.
2. **TradeFacade Admission & Execution Conflict Hardening (`app/trading/facade.py`)**:
   - Caught `(repository.AdmissionError, pymongo.errors.PyMongoError)` in `submit_trade()`.
   - On concurrent collision or transaction conflict, checks if winning transaction committed the intent for that idempotency key. If found, returns `ALREADY_PROCESSED` with committed fill/order details.
   - Caught `IntentExecutionRejected` (for already-consumed intents) and `PyMongoError` around `execute_intent()`, resolving duplicate responses cleanly.
3. **Repository Slot Conflict Handling (`app/trading/attribution/repository.py`)**:
   - Replaced flawed `except Exception: slot_col.find_one(..., session=s)` with explicit `raise SlotConflictError(...)`, cleanly aborting the transaction without secondary `NoSuchTransaction` failures.
4. **Harness Alignment (`tests/integration/test_control_plane_mongo_harness.py`)**:
   - Updated integration scenarios to comply with the hardened execution boundary (parent policy decisions, quote ages, and active risk reservations/slots).

---

## 3. Version Tracking

- **Source / Primary Commit**: `cc4a2cd5` (`master`)
- **Remote Head**: `origin/master` @ `cc4a2cd5`
- **Deployed Version**: `trading-service:cc4a2cd5` on Synology NAS (`10.0.0.16:3031`)
- **Live Deployed SHA Check**: Verified `deployed_commit_sha: "cc4a2cd5"` via `/control-plane/metrics`

---

## 4. Exact Validation Commands & Results

1. **Step 05 Real Concurrency & Invariant Test Suite**:
   - Command: `TRADING_BOT_MONGO_TEST=1 /home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest -v tests/integration/test_real_concurrency_and_invariants.py`
   - Results: **12 passed in 66.42s** (0 failed, 0 skipped).
2. **Integration Regression Harness Suites**:
   - Command: `TRADING_BOT_MONGO_TEST=1 /home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest -v tests/integration/test_control_plane_mongo_harness.py tests/integration/test_shadow_execution_atomicity.py`
   - Results: **20 passed in 96.96s** (0 failed, 0 skipped).
3. **Unit Regression Suites**:
   - Command: `/home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest -v tests/unit/test_execution_boundary_hardening.py tests/unit/test_diagnostics_resilience.py tests/unit/test_trade_facade.py`
   - Results: **27 passed in 1.09s** (0 failed, 0 skipped).
4. **Pre-Commit Secret Verification Scan**:
   - Command: `git diff --cached -i -G"(password|secret|token|api_key|credential)"`
   - Results: Verified zero static credentials in git diff.
5. **NAS Container Deployment & Live Verification**:
   - Command: `export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"; npm run deploy -- --skip-pull`
   - Build & Deploy Time: 147s
   - Endpoint Checks:
     - `curl -s -i http://10.0.0.16:3031/health` -> `HTTP/1.1 200 OK`, `{"status":"ok","service":"trading-service","version":"v3"}`
     - `curl -s -i http://10.0.0.16:3031/control-plane/metrics` -> `HTTP/1.1 200 OK`, `x-metrics-status: HEALTHY`, `deployed_commit_sha: "cc4a2cd5"`, `outbox_worker` and `outcome_worker` both `ALIVE`.

---

## 5. Rollback & Invariant Verification

- **Crash Invariant**: Any mid-transaction failure rolls back completely, preserving bot cash balances, position counts, and tax lot integrity.
- **Rollback Image**: `trading-service:previous` saved on Synology NAS before container swap.

---

## 6. Unresolved Issues

None. All 9 required scenarios pass against real MongoDB replica set infrastructure.

---

## 7. Next Unlocked Step

- **Step 06 — Freeze the shared outcome contract** is now **UNLOCKED and READY**.
- Steps 07–30 remain **LOCKED**.
