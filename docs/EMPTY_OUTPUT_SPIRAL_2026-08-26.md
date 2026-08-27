# Caller-side hardening for the empty-output death spiral (2026-08-26)

Companion to lazy-agent-service `docs/EMPTY_OUTPUT_SPIRAL_2026-08-26.md`,
which carries the root cause (a 60 s = 60 s timeout tie turning slow tools
into MCP `-32001` protocol errors) and the mechanism proof (the stacked
`<empty-output-recovery>` system messages reproduce content=0 3/3 against
Gold Spark; the rewritten tail answers 3/3). This repo got the caller-side
half, shipped in `64fffb0`.

## What was wrong here

- **Every circuit-breaker abort lied**: "Circuit breaker tripped: phase
  'quant_analyst' failed 0 time(s) with outcomes []" (seen on the 2026-08-05
  KSS abort and again this week). `_check_abort` built the reason BEFORE
  `record_outcome`, and the attempt consumed by `should_retry` was never
  ledgered anywhere.
- **A dead desk paged nobody.** The abort path wrote one log line and a
  HOLD@0 noop row — indistinguishable from a quiet decision on the dashboard.
- The junior prompt told the agent tools fail sometimes, but nothing warned it
  that prism's `<tool-retry-guidance>` would demand argument fixes for a
  timeout that has none.

## The fix

- `_check_abort` records the outcome first; `_run_agent_with_circuit_breaker`
  ledgers the attempt it retries away from. Abort reasons now read
  `failed 2 time(s) with outcomes ['AGENT_ERROR', 'AGENT_ERROR']`. The
  `_ABORT_MARKERS` substrings ("V3 Pipeline aborted", "Circuit breaker
  tripped") are unchanged, so `disposition.py` still classifies aborts.
- New `degraded_alert.alert_phase_abort` (`v3_phase_abort`, 12 h dedupe,
  never raises) fires on both circuit-breaker and timeout aborts.
  <!-- check: grep -q "alert_phase_abort" app/services/degraded_alert.py -->
- `junior_analyst.py` RULES: a TOOL_TIMEOUT is not an argument problem; after
  one TOOL_TIMEOUT or two failed calls, stop calling tools and emit the
  artifact from pre-collected data. (Pairs with the new structured
  TOOL_TIMEOUT results from lazy-agent-service.)

Tests: `tests/unit/test_phase_abort_visibility.py` — drives the real
`_run_agent_with_circuit_breaker` → `_check_abort` seams, verified red on
pre-fix code (the truth test fails on the literal "0 time(s)" string).
Full suite green: 5,245 passed / 74 skipped (NAS-gated) /
`test_dynamic_trigger_normalisation` is the known `-n` flake, passes alone.

## Deliberately NOT changed (with reasons)

- **No raise-on-empty in `base_agent`.** The existing ladder (empty sentinel →
  repair pass → breaker retry → DATA_GAP degrade) already handles an empty
  200, and a raise would ride the 5× `aresilient_call` envelope: worst case
  5 × 2 × 2 = 20 `/agent` requests per phase, each able to leave a zombie
  server-side loop (prism `persistOnDisconnect: true`, and trading-service has
  no cancel path).
- **Prism untouched** — read-only upstream. The `-32001` ceiling, the retry
  guidance text, and the recovery loop's message shape are all worked around
  at seams we own.

## Open items

- The primer "Acknowledged. I am ready to process the quantitative data."
  appeared in the V3 junior conversation, but the V3 path
  (`AgentHarness`/`ConversationSession`) does not insert it —
  `prism_agent_caller.py:502` (non-V3) does. Provenance unresolved; do not
  build on it.
- Two disagreeing turn budgets for junior_analyst: 7
  (`tool_whitelists.py`, the live V3 path) vs 10 (`guardrails.py`
  `AGENT_ROLE_BUDGETS`, read only by `prism_agent_caller`).
- Retry amplification (the 20-requests worst case above) is documented, not
  capped.
