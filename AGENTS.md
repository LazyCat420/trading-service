# AGENTS.md — V3 Trading Pipeline Harness Rules

> **This file is the single source of truth for all harness-level constraints.**
> Every value in this file has a corresponding enforcement in Python source.
> If this file and the code disagree, the code is the truth — update this file.

---

## 1. Pipeline Architecture

The V3 pipeline is a **4+1 layer linear state machine**:

```
Layer 1: Context Init    → SharedDesk created, cycle_metadata + data_report + memory_context injected
Layer 2: Research         → Junior Analyst → Fundamental Analyst → Quant Analyst (sequential)
Layer 3: Debate           → Bull → Bear → Bull Defense (sequential, SharedDesk-mediated)
Layer 4: Decision         → Regime Engine → Board of Directors (persona-swapped by regime)
Layer 5: Synthesis        → Decision Synthesizer (optional, controlled by DECISION_AGENT_ENABLED)
```

**Phase transitions** are enforced by `SharedDesk.advance_phase()`:
- `INIT → RESEARCH_DONE → DEBATE_DONE → PM_DONE`
- Any phase can transition to `ABORTED` (terminal)

**Source**: `app/v3/shared_desk.py`, `app/v3/orchestrator.py`

---

## 2. Agent Budget Limits

| Agent | Max Turns | Max Tool Calls | Model Type |
|---|---|---|---|
| Junior Analyst | 10 | 15 | Tools-enabled |
| Fundamental Analyst | 12 | 20 | Tools-enabled |
| Quant Analyst | 12 | 20 | Tools-enabled |
| Bull Agent | 3 | 0 | Pure reasoning |
| Bear Agent | 3 | 0 | Pure reasoning |
| Regime Engine | 5 | 8 | Tools-enabled |
| Board of Directors | 5 | 0 | Pure reasoning |
| Decision Synthesizer | 7 (default) | 10 (default) | Pure reasoning |

**Enforcement**: `V3AgentBudget.is_exhausted()` checks turns AND tool calls.

**Token tracking**: `consume_tokens()` is **informational only** — it does NOT enforce a hard limit. The actual output cap is `max_tokens=8192` passed to the LLM call in `agent_runner.py`.

> **Phase 1 Note**: Prompt-level iteration limits ("MUST NOT make more than 5 tool calls") have been removed from all agent prompts. The `V3AgentBudget` system is now the **single source of truth** for all budget enforcement.

**Source**: `app/v3/guardrails.py` lines 74-82

---

## 3. Context Size Limits

| Limit | Value | Purpose | Source |
|---|---|---|---|
| `_MAX_SUMMARY_CHARS` | 2,000 chars | Per-artifact compression for downstream agents | `guardrails.py:173` |
| `_MAX_COMPRESSED_CONTEXT_CHARS` | 8,000 chars | Total SharedDesk narrative passed to any agent | `shared_desk.py:64` |
| `max_tokens` (LLM output) | 8,192 tokens | Hard cap on LLM response length | `agent_runner.py:161` |

**Design**: These are layered intentionally. Each artifact is compressed to ≤2,000 chars. With up to 9 artifacts, the worst case is ~18,000 chars, so the 8,000 outer limit truncates the combined narrative to prevent context snowball.

---

## 4. Circuit Breaker Rules

- **Max retries per phase**: 1 (configurable via `CircuitBreaker(max_retries_per_phase=1)`)
- **Retryable outcomes**: `TOOL_OUTAGE`, `AGENT_ERROR`
- **Non-retryable outcomes**: `SUCCESS`, `DATA_GAP`, `TIMED_OUT`
- **Abort behavior**: When retries are exhausted, the pipeline produces a `HOLD` with `confidence=0` and `triage_tier="v3_aborted"`

**Source**: `app/v3/guardrails.py` lines 217-301

---

## 5. Tool Loop Detection

- **Max identical failures**: 3 (same tool, same args hash, same failure status)
- **Action on detection**: Injects a `[SYSTEM OVERRIDE]` message telling the agent to stop calling the tool and reason from existing data
- **Scope**: One `ToolLoopDetector` per agent run (not shared across agents)

**Source**: `app/v3/guardrails.py` lines 105-166

---

## 6. Recursion Guard

- **Mechanism**: Module-level `_active_v3_sessions` set in `guardrails.py`
- **Key format**: `{cycle_id}:{ticker}:{agent_name}`
- **Limitation**: Process-local only. Safe for single-process asyncio deployment. **Will silently break if multi-worker or multi-replica deployment is used.**
- **Current deployment**: Single process via `cycle_main.py` → asyncio event loop

**Source**: `app/v3/guardrails.py` lines 308-328

---

## 7. Debate Rules

The debate is a **linear state machine**, not a parallel fan-out:

1. **Bull Agent** runs first with `include_debate_context=True` (sees research artifacts)
2. **Bear Agent** runs second with `include_debate_context=True` (sees research + bull argument)
3. **Bull Defense** runs third with `include_debate_context=True` (sees research + bull + bear)

**Gate conditions**:
- Bear only runs if `desk.has_artifact("bull_argument")` is True
- Defense only runs if BOTH `bull_argument` AND `bear_rebuttal` exist

**Source**: `app/v3/orchestrator.py` lines 176-226

---

## 8. Anti-Hallucination Rules (Shared Across All Agents)

All agent prompts MUST incorporate these blocks from `app/config/guardrails.py`:

- **ANTI_HALLUCINATION_BLOCK**: Do NOT fabricate data. "Silence is better than fiction."
- **PEER_ACCOUNTABILITY_BLOCK**: Flag other agents' fabrications with `FABRICATION ALERT`.
- **DATA_MISSING_PROTOCOL**: Mark missing data as `DATA_MISSING`, set `proceed=false`.
- **DEPTH_OF_ANALYSIS_BLOCK**: Show your work, connect dots, second-order thinking.
- **CONVICTION_THRESHOLD_BLOCK**: No BUY without a 3-5 year thesis.
- **DEVIL_ADVOCATE_BLOCK**: Steelman the opposing position before concluding.

**Source**: `app/config/guardrails.py` lines 15-77

---

## 9. Forbidden Actions (Global)

These constraints apply to ALL agents in the V3 pipeline:

1. **No agent may call a tool it is not whitelisted for.** Tool access is controlled by `TOOL_WHITELIST` per agent module.
2. **Pure reasoning agents (Bull, Bear, Decision Synthesizer) have ZERO tools.** If the LLM attempts a tool call, it will be rejected. **Board of Directors** has limited tool access (`get_portfolio_state`, max 3 calls) as of Phase 2.
3. **No agent may directly communicate with another agent.** All inter-agent data flows through the SharedDesk via typed artifacts.
4. **No agent may modify another agent's artifact.** Artifacts are append-only on the SharedDesk.
5. **No agent may spawn another agent.** Only the orchestrator may invoke agents. The recursion guard enforces this.

---

## 10. Deployment Constraints

- **Single-process only**: The `_active_v3_sessions` recursion guard is a process-local Python set. Multi-worker deployments (Gunicorn workers, Docker scale) will break this guard silently.
- **No live trading**: The V3 pipeline produces `trade_decision` artifacts and persists them to the `trade_results` DB table. There is no broker integration or order execution path.
- **DECISION_AGENT_ENABLED**: Defaults to `True` in `app/config/config.py`. Controls whether Layer 5 (Decision Synthesizer) runs.

---

## 11. Memory System Integration

The V3 pipeline reads from and writes to long-term memory:

- **Layer 1 (Read)**: `MemoryRetriever.retrieve(ticker)` fetches past cycle observations. Results are formatted via `build_memory_brief()` and injected into `cycle_metadata["memory_context"]`, which appears in each agent's user prompt as `## Past Cycle Memory`.
- **Post-Pipeline (Write)**: After `save_desk()`, an episodic observation is recorded via `MemoryStore.add_episodic_observation()` with the cycle outcome (action, confidence, regime, reasoning).
- **Non-Fatal**: All memory calls are wrapped in try/except. Memory system failures do NOT abort the pipeline.

**Source**: `app/services/memory/retriever.py`, `app/services/memory/store.py`, `app/v3/orchestrator.py`

---

## 12. Strategy Tracking

After `save_trade_result()`, the pipeline calls `strategy_tracker.record_strategy()` with `agent_prompt_hash="v3_pipeline"`. This enables P&L tracking per pipeline version.

- Only BUY and SELL signals are recorded (HOLD is skipped by design)
- Non-fatal: wrapped in try/except

**Source**: `app/trading/strategy_tracker.py`, `app/v3/orchestrator.py`

---

## 13. Data Report Size Cap

The pre-collected data report (`data_report.py`) is capped at **15,000 characters** to prevent context window overflow on smaller models.

**Truncation priority** (drops lowest priority first):
1. Market Data & Fundamentals (keep)
2. Technical Indicators (keep)
3. News & Sentiment (keep)
4. Reddit Social Sentiment (drop first)
5. YouTube Transcripts (drop second)

**Source**: `app/v3/data_report.py` — `_MAX_DATA_REPORT_CHARS = 15000`

---

## 14. Inactive Agents

- **`portfolio_manager.py`**: INACTIVE. Part of V2 scoring/gatekeeper system. Registered with Prism but NOT invoked by V3 orchestrator. Reserved for future Layer 6 (Portfolio Optimization).
