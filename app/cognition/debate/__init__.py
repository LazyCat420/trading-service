"""
Debate & Adjudication System (Dev 3).

Implementation of the structured research committee.

Modules:
  - debate_coordinator: helpers only. The classic adversarial Bull/Bear
    pipeline it is named for was deleted 2026-07-29 (no production caller);
    three helpers used by `tournament` remain.
  - tournament: 4-stage Tournament Debate (Pitch → Backtest → H2H → Jury)
  - equation_library: Shared Quant Equation storage and sandboxed executor
  - backtest_runner: Deterministic backtest filter for tournament stages
  - format_validator: Strict CEE (Claim-Evidence-Equation) format enforcement
  - debate_judge: Final verdict judge for classic debate mode
  - action_gate: Position-aware action validation
"""
