# Trading Pipeline Evidence Delivery & Shared Desk Hardening Benchmark

**Date**: 2026-09-14  
**Baseline Verification Cycle**: `cycle-v3-1789419565` (Target: `NVDA`, Mode: `paper`, Duration: 21m 44s, Outcome: `BUY @ 72%`, Position: `1.0%`)  
**Commit Range**: `92695074..HEAD`  

---

## Executive Summary

Following the live trading cycle verification and audit in `sun/.agents/AUDIT-board-evidence-delivery-verification-2026-09-14.md`, five key operational and architectural findings were addressed:

1. **Decision Synthesizer Regime Evidence Delivery**:
   - *Root Cause*: `SharedDesk.get_compressed_context()` enforces a 10,000 character budget (`_MAX_COMPRESSED_CONTEXT_CHARS`). Research and debate prose across 7+ upstream agents easily exceed the budget. `## Market Regime` was placed at the bottom of `sections`, so `combined[:available]` sliced it away. Commit `183866e6` protected the Board via `regime_packet` dynamic section injection (`_KEEP=0`), but left the Decision Synthesizer reliant on truncated compressed context.
   - *Remediation*:
     - In `app/v3/shared_desk.py`: Moved `## Market Regime` to the head of `sections` before research artifacts so that research prose absorbs any truncation cuts, never the governing regime directive.
     - In `app/v3/agent_runner.py`: Extended `regime_packet` injection (`_KEEP = 0`) to `v3_decision_synthesizer` and passed `include_regime=False` to `get_compressed_context()`, emitting `synthesizer.regime_delivery` telemetry.
2. **Whiteboard Data Sharing & Performance Ablation**:
   - *Empirical Finding*: In the live run, 13 entries were mirrored to `whiteboard_entries` by backend listeners, but **zero (0)** `whiteboard_read` or `whiteboard_write` tool calls were made by any of the 12 agents. 100% of inter-agent evidence is delivered via `SharedDesk` + `dynamic_sections`.
   - *Ablation Suite*: Added `tests/unit/test_whiteboard_ablation.py` proving that deciders execute with identical validity, complete evidence, and zero data loss without whiteboard tools.
3. **Analyst Scraper Resilience Against Anti-Bot Domains**:
   - *Finding*: In the live run, 1 tool call failed with `HTTP 403` when scraping anti-bot protected sites.
   - *Remediation*: Added explicit system prompt instructions in `v3_junior_analyst` and `v3_fundamental_analyst` forbidding scraping anti-bot protected domains (e.g. `seekingalpha.com`, `finance.yahoo.com`, `bloomberg.com`) and directing models to pre-collected desk data, SEC/EDGAR tools, or structured `DataGap` logging.
4. **End-to-End Cycle Wall Clock Accounting**:
   - *Finding*: Historical reports and `cycle_audit.py` recorded `trade_ms` (which is `None` or only ~68s for the synthesizer), falsely reporting `missing: trade_ms` on observe/paper cycles.
   - *Remediation*: Updated `scripts/cycle_audit.py` to display `total_ms` (e.g. `total=1,303,948ms`) alongside stage timings and recognize paper/observe mode without false-positive missing warnings.

---

## Test Verification

- `tests/unit/test_board_evidence_delivery.py`: 8 passed in 0.40s.
- `tests/unit/test_whiteboard_ablation.py`: 3 passed in 0.37s.
- `tests/unit/test_shared_desk.py`: 25 passed.
- `tests/unit/test_shared_desk_debate_context.py`: 22 passed.
- `tests/unit/test_dynamic_regime.py`: 9 passed.
- **Total Unit Suite**: 55 passed in 1.11s.
- **Cycle Audit Output**:
  ```
  PASS phase timings recorded total=1,303,948ms (collect=0ms, analyze=1,282,503ms, trade=paper/observe) | missing: none | tokens=487,306 collector_skip=66.7%
  ```
