# Decision Synthesizer Context Efficiency & Unified Evidence Packet — September 15, 2026

## Executive Summary
Audited and resolved the multi-turn context ballooning and redundant `whiteboard_read` tool calls in `v3_decision_synthesizer`. In historical benchmarks (e.g., BHP canary), synthesis consumed up to **37% of cycle tokens** (464,860 tokens) across 13 model turns due to truncated context pointers inviting 18 repetitive tool lookups.

---

## 1. Root Cause Analysis
1. **Premature Truncation**: `get_compressed_context()` in `app/v3/shared_desk.py` clamped combined desk research and debate to 10,000 characters, appending `[research context TRUNCATED; read full artifact]` and `OMITTED: X complete record(s); full artifact required. Use whiteboard_read before judging it unanswered.`
2. **Whiteboard Summarize Pointers**: `whiteboard.summarize()` in `app/agents/whiteboard.py` injected `... [TRUNCATED - whiteboard_read('{section}') for full content]` into the prompt.
3. **Conversational History Re-processing**: Every tool call in `agent_runner.py` appended messages to the prompt history. Across 13 turns, serialized context grew from 72k to 195k characters, reprocessing 1,964,858 characters (464k tokens) repeatedly.

---

## 2. Changes Implemented

### 2.1 Complete Synthesis Evidence Packet (`app/v3/synthesis_evidence.py`)
- Created `build_synthesis_packet(desk)` delivering an authoritative, un-truncated view of:
  - **Manifest**: Section inventory with status (`COMPLETE` / `EMPTY` / `UNAVAILABLE`), SHA-256 digests, and byte counts.
  - **Board of Directors Verdict**: Full `final_decision` artifact (action, confidence, position size, stop/take-profit, dynamic trigger, reasoning).
  - **Debate Verdict & Propositions**: Full `tournament_result` or `debate_judge` verdict, plus structured debate propositions and unclipped bull defense concessions/independent-risk answers.
  - **Adversarial Arguments**: Complete Bull and Bear claims.
  - **Research & Verified Metrics**: Full findings from Junior, Fundamental, Quant, and Valuation analysts.
  - **Whiteboard Annotations**: Active teammate notes queried directly from MongoDB.
- Emits telemetry trace event `synthesizer.evidence_delivery`.

### 2.2 Orchestrator / Runner Integration (`app/v3/agent_runner.py`)
- Injected `synthesis_packet` into `dynamic_sections` with `_KEEP` priority for `v3_decision_synthesizer`.
- Omitted duplicate truncated `SharedDesk Context Summary` and truncated `whiteboard.summarize` for `v3_decision_synthesizer`.

### 2.3 System Prompt Refinement (`app/v3/agents/decision_agent.py`)
- Instructed `v3_decision_synthesizer` that all SharedDesk evidence is delivered in full with an authoritative manifest, and instructed it not to call `whiteboard_read` for sections verified as `COMPLETE`.

---

## 3. Verification & Deployment
- **Unit Tests**: `tests/unit/test_synthesis_evidence_packet.py` (3/3 passed). Full regression suite across modified components: 48/48 passed in 2.11s.
- **Commit**: `ede0852f` merged to `master` and pushed to GitHub (`LazyCat420/trading-service`).
- **NAS Deployment**: Deployed via `npm run deploy` to Synology NAS (`10.0.0.16:3031`).
- **Container Health**: Container status `running (health: healthy)`; HTTP `/health` responded `200 OK`.
