# Board Contract & Pipeline Resilience: Verification & Testing Plan
Date: 2026-09-16
Status: Active
Scope: `trading-service` (V3 Agentic Pipeline)

## 1. Live Validation Evidence (Cycle `cycle-v3-1789527015`)
- Target: `BTC`
- Board Phase:
  - Attempt 1: Accepted cleanly (`artifact.accepted`, `repaired: null`)
  - Decision: `action: HOLD`, `confidence: 62`, `timing: null`
  - Trace Events: Zero rejections (`v3_contract_rejected_BTC` count: 0)
  - Outcome: `SUCCESS` in 58 seconds
- Synthesizer Phase:
  - Attempt 1: Accepted (`repaired: true`)
  - Outcome: `HOLD @ 62%` (regime: `CONTRADICTORY`, persona: `jane_street`)
- Cycle Outcome: Completed end-to-end cleanly with status `done` in 927.1s.

## 2. Proposed Testing Matrix

### Layer 1: Unit & Deterministic Contract Tests (`tests/unit/`)
1. `test_nonentry_timing_normalization.py`:
   - Validates that `HOLD` with triggers missing `purpose` automatically resolves to `purpose="monitor"`.
   - Validates that `BUY`/`SELL` with missing timing or purpose is properly rejected.
2. `test_phase_abort_visibility.py`:
   - Validates that `is_retry=True` contract rejections return `PhaseOutcome.DATA_GAP` instead of `AGENT_ERROR`, preventing the circuit breaker from tripping.
3. `test_reasoning_step_preservation.py`:
   - Validates that retry attempts preserve step identity even when the LLM rephrases surrounding text.

### Layer 2: Integration & Fault Injection
1. Failure mode test: Intentionally inject a broken contract on Attempt 1 and Attempt 2; confirm the circuit breaker does NOT trip and synthesis degrades gracefully.
2. Regime coverage: Test against alternate market regimes to verify entry actions (`BUY` / `SELL`) enforce timing contract fields (`entry_window`, `entry` triggers).
