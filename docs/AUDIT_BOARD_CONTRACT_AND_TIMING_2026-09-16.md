# Audit & Resolution: Board of Directors Timing Contract & Circuit Breaker Abort (2026-09-16)

**Incident Date**: 2026-09-15 / 2026-09-16  
**Cycle ID**: `cycle-v3-1789490341` (BTC)  
**Status**: Resolved & Verified  
**Affected Tickers**: `BTC`, `CRWV`, `CRDO`, `CSCO`, `ADBE`, `MTD`, `SCHD`, `COIN`, `TH`, `PURR`

---

## 1. Incident Summary

During trading cycle `cycle-v3-1789490341`, ticker `BTC` aborted before reaching Layer 5 with the alert:
```
⚠️ BTC: no decision this cycle — V3 Pipeline aborted: Circuit breaker tripped: phase 'board_of_directors' failed 2 time(s) with outcomes ['AGENT_ERROR', 'AGENT_ERROR']. Retries: 1/1.
```

Forensic investigation in MongoDB (`pipeline_trace_events`, `pipeline_cycles`, `decision_telemetry`) revealed that the Board of Directors produced two consecutive contract rejections, tripping `CircuitBreaker.should_abort()` and terminating the pipeline for BTC. Cross-cycle analysis revealed 9 other tickers in recent cycles that failed with the identical pattern.

---

## 2. Root Cause Analysis

The failure was caused by a compound failure across four distinct layers:

### Layer 1: Underspecified HOLD Timing in Board Prompt
In `_BOARD_COMMON` (`app/v3/agents/board_of_directors.py`), the prompt detailed `BUY` timing (`enter_on_condition` vs `enter_now`), but did not document the required contract labels for `HOLD` decisions watching a dynamic trigger (`entry_mode="watch_only"`, `trigger_purpose="monitor"`). Consequently, the Jane Street persona emitted `HOLD` with a valid trigger (`sma_20_rise`), but omitted `trigger_purpose` in Attempt 1, and set `trigger_purpose: "entry"` in Attempt 2.

### Layer 2: Tie-Breaking in `unique_nonentry_timing_correction`
`unique_nonentry_timing_correction` (`app/v3/decision_contract.py`) was designed to auto-heal non-entry timing labels without calling the LLM. However, when a `dynamic_trigger` was present, both `monitor` and `research` were candidate 1-field patches. Because both patches were minimal (`len == 1`), the function treated them as an ambiguous tie (`monitor/research tie`) and returned `None`, falling through to model repair.

### Layer 3: The Contract Correction Reasoning Catch-22
When model repair was invoked, the repair prompt (`agent_runner.py:2021`) instructed:
```
Preserve action, confidence, reasoning, position size, stop loss, take profit and cited evidence exactly.
```
Under `financial_reasoning_version: 2`, `reasoning` is a deterministic, code-rendered string from `reasoning_steps`. In Attempt 2, the LLM corrected the timing fields, but echoed the 1,802-character `reasoning` string with the final sentence duplicated. 
This triggered two rejections:
1. `financial_reasoning.py:170`: Flagged `"reasoning conflicts with the selected code-verified steps; remove authored financial prose."`
2. `decision_contract.py:186`: Flagged `"correction changed reasoning"`.
Because `correction_errors` checked literal byte-equality of the `reasoning` string rather than verifying preservation of `reasoning_steps`, a valid correction was rejected.

### Layer 4: Unconditional Abort on Retry Contract Failure
In `agent_runner.py:2087`, contract rejection unconditionally returned `PhaseOutcome.AGENT_ERROR`. After retry exhaustion (`is_retry=True`), `CircuitBreaker.should_abort()` tripped, and `orchestrator.py:1789` aborted the pipeline, dropping the ticker completely before Layer 5 could evaluate it.

---

## 3. Remediation & Implementation

### 1. `app/v3/agents/board_of_directors.py`
- Added explicit `## TIMING & TRIGGER CONTRACT` section in `_BOARD_COMMON` defining exact rules for `BUY`, `HOLD` (`watch_only` + `monitor` for triggers; `watch_only` + `none` without), and `SELL`.

### 2. `app/v3/decision_contract.py`
- Updated `unique_nonentry_timing_correction`: For `HOLD` decisions with an evaluable `dynamic_trigger`, resolves the candidate tie to `monitor`.
- Updated `correction_errors`: When `financial_reasoning_version == 2`, verifies preservation of `reasoning_steps` rather than byte-equality of the code-rendered `reasoning` string.

### 3. `app/v3/agent_runner.py`
- In `run_v3_agent`: When `candidate.get('reasoning_steps') == original_artifact.get('reasoning_steps')`, pops `reasoning` before calling `render_reasoning_artifact` so `reasoning` is re-rendered deterministically from verified steps.
- At line 2087: Degrades to `PhaseOutcome.DATA_GAP` when `is_retry=True`, matching analyst degrade semantics. This allows `orchestrator.py:1800` to record the explicit degraded sentinel and allows Layer 5 to evaluate the ticker rather than dropping it.

### 4. Tests
- `tests/unit/test_nonentry_timing_normalization.py`: Added tests verifying that evaluable triggers on `HOLD` normalize to `monitor`, replaying BTC Attempt 1 artifact, and testing `reasoning_steps` preservation under v2.
- `tests/unit/test_phase_abort_visibility.py`: Added test verifying that retry degradation to `DATA_GAP` avoids tripping the circuit breaker.

---

## 4. Verification

- All 134 focused tests across `test_nonentry_timing_normalization.py`, `test_frozen_board_failures.py`, `test_financial_evidence.py`, `test_financial_reasoning.py`, `test_financial_edge_cases.py`, and `test_phase_abort_visibility.py` pass.
- BTC Attempt 1 payload normalizes cleanly to `trigger_purpose: 'monitor'` with 0 contract errors.
- BTC Attempt 2 candidate with duplicated prose passes `correction_errors` with 0 errors.
