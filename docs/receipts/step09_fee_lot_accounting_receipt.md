# Completion Receipt — Step 09: Fix Realized Fee and Lot Accounting

- **Step ID**: `09`
- **Title**: Fix realized fee and lot accounting
- **Owner**: LazyCat420
- **Start Timestamp**: `2026-09-17T19:35:00Z`
- **End Timestamp**: `2026-09-17T19:50:00Z`
- **Status**: `COMPLETE`

---

## 1. Observed Baseline & Gap Identification

Prior to Step 09:
1. In `app/trading/executor.py`, `COLL_LOT_CLOSURES` populated `fees` solely from the exit order fill fees (`fees * (close_amt / qty)`). The entry fee paid when opening the lot was completely omitted from realized net P&L.
2. In `COLL_POSITION_LOTS`, `entry_fee` and `remaining_entry_fee` were not tracked as distinct attributes on tax lots.
3. In `LotAlphaEvaluator.evaluate_lot_closure` (`app/trading/attribution/evaluator.py`), `fee_drag_pct` was computed as `(fees / notional) * 100.0` with `notional = exit_px * qty`, and `net_return` was calculated as `gross_return - fee_drag_pct`. For 10 @ 100 with fee 1 sold @ 110 with fee 1, this yielded `9.8%` (relative to $1,000) instead of `98 / 1001 = 9.7902%` (relative to total invested capital).
4. In `evaluate_closed_lot_alpha_iteration` (`app/trading/attribution/worker.py`), missing lots defaulted to `provenance="LIVE"` and `provenance_complete=True`, causing unlinked or corrupted closures to be erroneously attributed as valid live alpha.
5. Multi-lot and partial FIFO sales lacked running balance conservation for entry and exit fees, risking sub-cent fee leakage across sequential executions.

---

## 2. Changes Made

1. **Lot Alpha Evaluator (`app/trading/attribution/evaluator.py`)**:
   - Refactored `LotAlphaEvaluator.evaluate_lot_closure`:
     - Accepts `allocated_entry_fee`, `exit_fee`, `qty`, `fees_embedded_in_fills`, and `is_closing_long`.
     - Uses consistent invested-capital denominator:
       $$\text{invested\_capital} = (\text{lot\_entry\_price} \times \text{qty}) + \text{allocated\_entry\_fee}$$
     - Calculates exact cash flow dollar P&L:
       $$\text{dollar\_pnl} = \text{qty} \times (\text{lot\_exit\_price} - \text{lot\_entry\_price}) - \text{allocated\_entry\_fee} - \text{exit\_fee}$$
     - Calculates realized net return:
       $$\text{net\_return} = \frac{\text{dollar\_pnl}}{\text{invested\_capital}} \times 100.0$$
     - Calculates fee drag percentage:
       $$\text{fee\_drag\_pct} = \text{gross\_return} - \text{net\_return}$$
     - Guarantees backward compatibility with legacy `fees` parameter.

2. **Domain Contract & Models (`app/trading/attribution/outcome_contract.py` & `models.py`)**:
   - Defined canonical `PositionLot` model with strict Pydantic validation (positive quantity/prices, non-negative entry and remaining fees, UTC datetimes).
   - Expanded `LotClosureRecordV4` with optional lifecycle and evaluation attributes (`gross_pnl`, `net_pnl`, `fees`, `entry_decision_id`, `exit_decision_id`, `entry_intent_id`, `exit_intent_id`, `alpha_evaluated`, `lot_alpha`, `exclusion_reason`, `retry_after`, `status`, `benchmark_status`) to enable lossless round-trip persistence.
   - Re-exported `PositionLot` in `app/trading/attribution/models.py`.

3. **Trade Execution Engine (`app/trading/executor.py`)**:
   - BUY Execution:
     - Populates `entry_fee`, `remaining_entry_fee`, `origin: "LIVE"`, and `provenance_complete: True` on all inserted tax lots in `COLL_POSITION_LOTS`.
   - SELL Execution:
     - Implements exact pro-rata entry fee allocation across partial lot closures:
       - Full remainder close: $\text{alloc\_entry\_fee} = \text{remaining\_entry\_fee}$; $\text{new\_remaining\_entry\_fee} = 0.0$.
       - Partial close: $\text{alloc\_entry\_fee} = \text{round}\left(\frac{\text{close\_amt}}{\text{initial\_qty}} \times \text{entry\_fee}, 4\right)$; updates $\text{remaining\_entry\_fee}$.
     - Implements exact exit fee allocation across multi-lot closures: final closing slice absorbs the exact remaining order exit fee, preventing rounding drift.
     - Persists `allocated_entry_fee`, `exit_fee`, total `fees`, `gross_pnl`, `net_pnl`, `dollar_pnl`, `invested_capital_denominator`, `net_realized_return`, `origin`, `provenance`, `provenance_complete`, and `is_attributable` in `COLL_LOT_CLOSURES`.

4. **Attribution Worker (`app/trading/attribution/worker.py`)**:
   - In `evaluate_closed_lot_alpha_iteration`:
     - Fails closed on missing lot records: sets `provenance = "MISSING_LOT"`, `provenance_complete = False`, `is_attributable = False`, and `exclusion_reason = "MISSING_LOT_PROVENANCE"`.
     - Excludes migration lots (`origin == "MIGRATION"`) from live attribution.
     - Passes exact allocated entry/exit fees and quantity into `LotAlphaEvaluator`.
     - Persists reconciled evaluation records to `lot_closure_evaluations` and updates `COLL_LOT_CLOSURES`.
     - Added optional `now` and `db` parameters for deterministic isolated testing.

5. **Historical Migrator (`app/trading/migration/lot_migrator.py`)**:
   - Added `entry_fee: 0.0` and `remaining_entry_fee: 0.0` initialization for reconstructed and migration lots.

---

## 3. Mathematical Reconciliation Proof (Exit Gate Specimen)

- **Scenario**: Buy 10 @ $100 with fee $1.00; Sell 10 @ $110 with fee $1.00.
  - Cash Outflow = $10 \times 100 + 1 = \$1001.00$
  - Gross Inflow = $10 \times 110 = \$1100.00$
  - Net Inflow = $1100 - 1 = \$1099.00$
  - Gross P&L = $10 \times (110 - 100) = \$100.00$
  - Net Realized P&L = $1099 - 1001 = \$98.00$
  - Total Initial Cost Basis / Invested Capital Denominator = $\$1001.00$
  - Net Return on Total Initial Cost:
    $$\frac{98.00}{1001.00} \approx 0.0979020979 \rightarrow 9.7902\%$$
  - Gross Return: $\frac{110 - 100}{100} = 10.0\%$
  - Fee Drag: $10.0\% - 9.7902\% = 0.2098\%$

---

## 4. Test Verification & Results

1. **Unit Test Suite (`tests/unit/test_fee_lot_accounting.py`)**:
   - Command: `/home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/unit/test_fee_lot_accounting.py -v`
   - Results: **10 passed in 0.25s** (0 failed, 0 skipped).
   - Coverage:
     - Benchmark specimen exact exit gate reconciliation (98/1001).
     - Flat trade (P&L = -fees, return = -fees/invested_capital).
     - Losing trade (P&L = gross_loss - fees).
     - Sequential FIFO partial closures fee conservation.
     - Multi-lot FIFO closure fee and cost conservation.
     - Fees embedded in fills mode (zero double-counting).
     - Fail-closed provenance gating for missing lots.
     - Fail-closed provenance gating for migration lots.
     - PositionLot Pydantic validation.
     - LotClosureRecordV4 round-trip validation.

2. **Real MongoDB Integration Test Suite (`tests/integration/test_fee_lot_accounting_mongo.py`)**:
   - Command: `TRADING_BOT_MONGO_TEST=1 /home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/integration/test_fee_lot_accounting_mongo.py -v`
   - Results: **4 passed in 28.53s** (0 failed, 0 skipped).
   - Coverage:
     - Real Mongo BUY execution creates tax lots with fees and LIVE provenance.
     - Real Mongo sequential partial closures conserve fees and cash ledger balance.
     - Real Mongo multi-lot closures conserve exit fees and remaining balances.
     - Real Mongo alpha worker provenance gating fails closed on missing lots.

3. **Attribution Regression Suite (Steps 06-09)**:
   - Command: `TRADING_BOT_MONGO_TEST=1 /home/lazycat/github/projects/sun/trading-service/.venv/bin/pytest tests/unit/test_outcome_contract.py tests/unit/test_horizon_price_provenance.py tests/unit/test_maturity_scheduling.py tests/unit/test_fee_lot_accounting.py tests/integration/test_fee_lot_accounting_mongo.py tests/integration/test_maturity_scheduling_mongo.py tests/integration/test_horizon_price_provenance_mongo.py tests/integration/test_outcome_contract_mongo.py`
   - Results: **54 passed in 46.91s** (0 failed, 0 skipped).

4. **Zero Credential Leakage Gate**:
   - Command: `git diff --cached -i -G"(password|secret|token|api_key|credential)"`
   - Results: Zero hardcoded secrets detected; dynamic in-memory generation used exclusively.

5. **Live NAS Deployment Verification**:
   - Transfer and restart: `master@b07307df` deployed via deploy-kit in 151s.
   - Health Endpoint: `curl -s http://10.0.0.16:3031/health` -> `{"status":"ok","service":"trading-service","version":"v3"}` (HTTP 200 OK).
   - Control-Plane Metrics: `curl -s http://10.0.0.16:3031/control-plane/metrics` -> `metrics_status: "HEALTHY"`, `deployed_commit_sha: "b07307df"`, `outbox_worker` and `outcome_worker` both `ALIVE`.

---

## 5. Exit Gate Checklist

- [x] Flat/winning/losing lot fixtures reconcile ledger cash, quantities, dollar P&L, and returns.
- [x] Benchmark specimen: 10 @ 100 + fee 1, sold @ 110 + fee 1 yields net P&L 98; return on total initial cost is 98/1001 (~9.7902%).
- [x] Partial FIFO closures allocate entry fees pro-rata with running balance conservation.
- [x] Multi-lot closures allocate exit fees across closure records without fee leakage.
- [x] Remaining-lot and closed-lot allocations conserve total costs within declared tolerance ($0.0001).
- [x] Missing lot provenance fails closed as `UNATTRIBUTABLE` / `EXCLUDED` with `MISSING_LOT_PROVENANCE`.
- [x] Migration lots are excluded from attributable live alpha.
- [x] Zero credential leakage verified via diff scanner.
- [x] Live container running commit `b07307df` on NAS with healthy metrics.

---

## 6. Next Step Status

- **Step 09**: `COMPLETE`
- **Step 10**: `UNLOCKED` — *Connect verified outcomes to all readers (attribution report, scorecard, learning exports, shadow comparison)*.
