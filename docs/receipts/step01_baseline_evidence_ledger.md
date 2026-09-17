# System Baseline and Evidence Ledger: Step 01

**Step ID**: 01 — Establish today's system baseline  
**Date**: 2026-09-17  
**Operating Mode**: Assess / Plan (Read-Only)  
**Authority**: Universal Evidence-Driven Blueprint / Sequential Plan Revision 3  

---

## 1. Executive Summary & Deliverable

This document establishes the dated system baseline and evidence index required by **Step 01** of the *Sequential developer plan: verified trading, agent-trained specialists, research-method assessment (Revision 3)*.

All data was captured via read-only queries against the live Synology NAS container (`http://10.0.0.16:3031`), the local git repository (`trading-service` at `/home/lazycat/github/projects/sun/trading-service`), and the primary MongoDB replica set (`10.0.0.16:27017` `rs0`).

---

## 2. System Version & Deployment Status

| Subsystem | Observed Value | Evidence Source |
|---|---|---|
| Local Repository Branch | `master` (clean, 0 uncommitted changes) | `git status` in `/home/lazycat/github/projects/sun/trading-service` |
| Local Commit SHA | `61e489fd` | `git rev-parse HEAD` |
| Remote Repository | `origin` -> `https://github.com/LazyCat420/trading-service.git` | `git remote -v` |
| Remote Tracking | In sync with `origin/master` | `git status` |
| Deployed Instance URL | `http://10.0.0.16:3031` | Synology NAS container `trading-service` |
| Deployed Health Status | `{"status":"ok","service":"trading-service","version":"v3"}` | HTTP GET `/health` |
| Deployed Commit SHA | `c9dd5a61` | HTTP GET `/control-plane/metrics` |
| Commit Delta | Local `61e489fd` includes documentation handoff (`docs: document control plane blockers remediation in handoff index`). Code logic identical. | `git log c9dd5a61..61e489fd` |
| Effective Default Mode | `OBSERVE` (fail-closed) | HTTP GET `/control-plane/metrics` |
| Active Bot Accounts | `test_bot` (active, cash balance `$33,870.53`); all other 6 bots (`bench`, `cycle-backend`, `test`, `lazy-trader-v4`, `bot-default`, empty) inactive. | `db.bots.find({"is_active": True})` |
| Pipeline Cycle Status | `status: done`, `cycle_id: cycle-v3-1789651801` (ran today from 13:30:01 to 15:27:58 UTC). No cycle running. | `db.pipeline_state.find_one({"singleton_id": "current"})` |
| Background Workers | `outbox_worker`: ALIVE (heartbeat age 1.7s)<br>`outcome_worker`: ALIVE (heartbeat age 1.5s) | HTTP GET `/control-plane/metrics` |
| Learning Health | 18 component records active in DB; agents and lesson indexing reporting active heartbeats. | `db.learning_health.find()` |

---

## 3. Database Inventory & Queue Aging Analysis

A complete census of the 26 critical control-plane and autoresearch collections was taken:

| Collection | Total Count | Min Timestamp / Date | Max Timestamp / Date | Operational Disposition |
|---|---|---|---|---|
| `pipeline_state` | 1 | 2026-09-17 15:27:58 | 2026-09-17 15:27:58 | Stable, idle state (`status: done`) |
| `v3_system_commands` | 1,109 | 2026-06-24 04:24:41 | 2026-09-17 13:30:01 | Full cycle command history |
| `bots` | 7 | 2026-05-20 05:15:49 | 2026-09-02 17:39:38 | 1 active (`test_bot`), 6 inactive |
| `runtime_parameters` | 40 | N/A | N/A | System parameters (e.g. `HMM_REGIME_MODE`) |
| `decision_artifacts` | 17 | 2026-09-17 04:52:36 | 2026-09-17 15:24:48 | Fresh from 2026-09-17 cycles. 0 mature under 7-day rule. |
| `decision_outcomes` | 2,861 | 2026-05-06 14:32:05 | 2026-09-17 15:33:50 | 28 verified, 49 pending, 74 unsupported, 3 unrecoverable. 141 v2, 10 v3, 2,710 legacy/unversioned. |
| `decision_evaluations` | 1,287 | 2026-05-22 07:05:04 | 2026-09-17 15:24:48 | Historical evaluations |
| `execution_intents` | 5 | N/A | N/A | Intents from recent test scenarios |
| `execution_outbox` | 0 | N/A | N/A | Queue clear, 0 pending, 0 poison |
| `execution_reconciliations` | 0 | N/A | N/A | No real reconciliations executed yet under OBSERVE |
| `trade_fills` | 86 | N/A | N/A | Historical paper fills |
| `positions` | 27 | N/A | N/A | Active bot holdings |
| `position_lots` | 67 | N/A | N/A | FIFO tax lots |
| `lot_closures` | 31 | N/A | N/A | Realized closures |
| `v3_research_queues` | 468 | 2026-08-08 02:58:58 | 2026-09-17 15:24:51 | 467 `deep_dive_queue`, 1 `lead_queue`; 461 pending, 6 failed, 1 completed. Aging up to 40 days. |
| `dossier_question_log` | 1,018 | N/A | N/A | Research question backlog |
| `agent_skill_candidates` | 0 | N/A | N/A | Staged candidates queue clear |
| `learning_delivery_receipts`| 1,200 | 2026-09-07 13:41:53 | 2026-09-17 15:26:23 | Learning delivery telemetry |
| `learning_gateway_receipts` | 6,405 | 2026-09-07 12:06:30 | 2026-09-17 16:25:02 | Gateway telemetry |
| `learning_artifact_receipts`| 1,124 | 2026-09-07 13:58:00 | 2026-09-17 15:27:54 | Learning artifacts telemetry |
| `learning_health` | 18 | 2026-09-17 13:46:22 | 2026-09-17 17:55:51 | Active component health records |
| `autoresearch_reports` | 763 | 2026-05-07 05:08:56 | 2026-09-17 15:28:32 | Historical reports |
| `attribution_reports` | 0 | N/A | N/A | Pending Step 10 connection |
| `agent_traces` | 25,628 | 2026-05-08 04:57:13 | 2026-09-17 15:27:18 | Traces generated during cycles |
| `eval_scores` | 25,629 | 2026-05-08 23:25:19 | 2026-09-17 15:28:32 | Deterministic eval grades |
| `decision_scores` | 749 | 2026-08-05 20:39:00 | 2026-09-17 14:46:38 | Scored cycle decisions |

### Queue Aging Analysis (`v3_research_queues`)
- Total items: 468
- Status distribution: `pending`: 461, `failed`: 6, `completed`: 1.
- Type distribution: `deep_dive_queue`: 467, `lead_queue`: 1.
- All pending items have `attempts: 0`.
- Age distribution: Earliest item generated 2026-08-08 (`cycle-v3-1786346325` for `AMAT`), latest 2026-09-17.
- **Finding**: The queue is not accumulating retry failures; rather, pipeline cycles continuously enqueue deep-dive questions, but no active consumer process is currently assigned to dequeue and process them.

---

## 4. Test Execution & Isolated-Mongo Fixture Verification

### Standard Regression Suite
Command:
```bash
.venv/bin/pytest -q -p no:cacheprovider \
  tests/unit/test_control_plane_blockers_phase{1,2,3,4,5,6}.py \
  tests/unit/test_trade_facade.py tests/unit/test_no_direct_trader_callers.py \
  tests/unit/test_outcome_evidence.py tests/unit/test_eval_engine.py \
  tests/unit/test_skill_optimizer_gate.py
```
- **Result**: `82 passed, 1 skipped, 1 warning in 3.11s`.
- Warning: `datetime.datetime.utcnow()` deprecation in `test_protective_exits_route_through_facade`.
- Skipped test: `tests/unit/test_outcome_evidence.py::test_real_mongo_learning_cohort_excludes_legacy_and_mixed_sources` (skipped by design when `TRADING_BOT_MONGO_TEST` is unset).

### Real Mongo Integration Suite
Command:
```bash
TRADING_BOT_MONGO_TEST=1 .venv/bin/pytest -q -p no:cacheprovider \
  tests/integration/test_control_plane_mongo_harness.py
```
- **Result**: `14 passed in 55.32s`.
- All 14 real-database scenarios (covering lineage, idempotency, policy rejection, quote staleness, crash before commit, outbox crash recovery, poison queues, multi-lot closures, slippage, missing benchmark, strict pydantic models, and facade enforcement) passed cleanly against `trading_bot_pytest` on replica set `rs0`.

### Isolated Mongo Fixture Analysis & Regression Gap Uncovered
Inspection of `tests/conftest.py` line 387 (`real_mongo` fixture):
- Pinned to `TRADING_MONGO_TEST_DB = "trading_bot_pytest"`.
- Explicit fail-safe prevents execution if configured against `trading_bot` or `prism`.
- Drops test database on fixture cleanup.
- Connects to `10.0.0.16:27017` `rs0` (`isWritablePrimary: True`).

**Critical Finding on Previously Skipped Test**:
When running the skipped test with `TRADING_BOT_MONGO_TEST=1`:
```bash
TRADING_BOT_MONGO_TEST=1 .venv/bin/pytest -q -p no:cacheprovider tests/unit/test_outcome_evidence.py -k test_real_mongo_learning_cohort_excludes_legacy_and_mixed_sources
```
The test **FAILED**:
```
> assert dashboard['contract']['version'] == 2
E AssertionError: assert 3 == 2
tests/unit/test_outcome_evidence.py:165: AssertionError
```
- **Root Cause**: `app/autoresearch/outcome_evidence.py:22` defines `CONTRACT_VERSION = 3`. The test fixture asserted legacy version 2. Because this test was marked `real_mongo` and skipped in standard CI, this discrepancy went unnoticed.

---

## 5. First Specialist-Task Decision Status

**Status**: **CONFIRMED OPEN**.  
No specialist task (e.g. fixed-horizon volatility estimator, input-quality classifier, or future event probability model) has been chosen or implemented. Steps 16–28 remain completely locked.

---

## 6. Step 01 Completion Receipt

- **Step ID**: `01`
- **Owner**: `LazyCat420` (backend developer)
- **Start Timestamp**: `2026-09-17T17:53:00Z`
- **End Timestamp**: `2026-09-17T18:03:00Z`
- **Status**: `BASELINE CAPTURED / EXIT GATE MET FOR STEP 01`
- **Observed Baseline**:
  - Source Commit: `61e489fd` (master)
  - Deployed Commit: `c9dd5a61` (NAS)
  - Control-Plane Mode: `OBSERVE`
  - Active Account: `test_bot` ($33,870.53 cash)
  - Cycle State: Idle (`done`)
  - Worker Status: `outbox_worker` ALIVE, `outcome_worker` ALIVE
  - Queues: 461 pending deep dive items with 0 attempts, aging up to 40 days
- **Changed Files**: None (Step 01 is read-only baseline establishment)
- **Validation Commands**:
  - `curl -s http://10.0.0.16:3031/health` -> HTTP 200 `status: ok`
  - `curl -s http://10.0.0.16:3031/control-plane/metrics` -> HTTP 200
  - `.venv/bin/pytest tests/unit/...` -> 82 passed, 1 skipped
  - `TRADING_BOT_MONGO_TEST=1 .venv/bin/pytest tests/integration/...` -> 14 passed in 55.32s
- **Unresolved Issues**:
  - Skipped unit test `test_real_mongo_learning_cohort_excludes_legacy_and_mixed_sources` has version mismatch (`assert 3 == 2`); logged for resolution under Step 06.
  - `v3_research_queues` deep dive items have no active consumer process; logged for resolution under Step 12.
- **Single Next Unlocked Step**: `02 — Make SHADOW execution atomic and isolated`
