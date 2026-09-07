# Memory-only benchmark — 2026-09-07

Removing the upstream memory bundle reduced measured input-token usage, but **did not demonstrate an overall improvement in completed work, output quality or reliability**. Some retained memory saved work; other records contaminated answers with another ticker's date or candidate list. The result supports selective, scoped memory evaluation, not blanket retention or an unconditional claim that removal is better.

The completed primary experiment contains **32 role attempts / 16 paired comparisons**, across eight cases and seven roles, with a second repetition reversing each pair's order. The sequential component probe contains **28 scored calls plus two excluded warmups**. Both finished; all 32 role attempts received production-parser scoring and manual evidence review. No live trades, learning updates or production-tool actions occurred.

## Results

“Before” retains the actual upstream memory; “after” removes it. Both arms retain identical current skills, role instructions, frozen research, tools and retrieval query.

| Measurement | Memory retained | Memory removed | Interpretation |
|---|---:|---:|---|
| Input tokens, 12 pairs with complete usage | 353,491 | 239,375 | **−32.3%** in this sample |
| Output tokens, those 12 pairs | 31,607 | 28,167 | **−10.9%**, including an attempt that produced no artifact |
| Mean elapsed time, all 16 attempts/arm | 247.12s | 177.19s | −28.3%; includes failures, omitted work and uncontrolled queueing |
| Median elapsed time, all attempts | 176.41s | 159.54s | −9.6% |
| Parser accepts artifact | 14/16 | 13/16 | Acceptance alone does not enforce required fields |
| First-pass usable artifact | **14/16** | **12/16** | Parser, shape, minimum schema and Board entry contract |
| Usable artifact plus mandatory Junior note | **14/16** | **11/16** | Restricted completion proxy; not all-role workflow compliance |
| Truncated-stream failures | 2/16 | 2/16 | No observed reduction |
| Turn-budget exhaustion | 0/16 | 1/16 | Removed-memory Bear, second repetition |
| Parsed artifact missing required schema field | 0/16 | 1/16 | Removed-memory STX Junior omitted `data_gaps` |
| Required Junior note omitted | 0/4 | 2/4 | Both removed-memory STX repetitions; overlaps the schema failure above |
| Matched supplied facts, completed pairs | 29/30 | 29/30 | Fundamental 17/17, Quant 10/10, Regime 2/3 on each side |
| Tool calls / explicit unavailable-fixture errors | 75 / 30 | 32 / 18 | Replay behavior, not live-tool reliability |

The four incomplete-usage pairs are excluded **on both sides** from the primary token totals. Failed requests remain in completion and all-attempt latency denominators. Total observed tokens across all attempts were 574,777 input / 37,218 output before and 267,311 input / 31,116 output after. These are **lower bounds**, because each arm has two requests with unknown usage; their apparent difference is not a valid full-workload cost estimate.

All four Board responses were recoverable HOLD decisions and passed the production entry contract. None exercised a BUY sizing limit. Each included extra thought-process prose despite the raw-JSON directive; those tokens count even though the parser recovered the artifact. No response hit the output-token cap. No schema-invalid tool arguments were observed.

### Compare completed work as well as completed usage

A separate, explicitly conditional diagnostic keeps only the **nine pairs where both arms produced a usable artifact and the required Junior note**:

| Measurement | Retained | Removed | Change with removal |
|---|---:|---:|---:|
| Input tokens | 277,015 | 179,015 | **−35.4%** |
| Output tokens | 24,797 | 26,002 | **+4.9%** |
| Mean elapsed time | 185.30s | 191.93s | **+3.6%** |

This subset excludes failures and is not an unbiased estimate of the whole workload. It nevertheless shows why “fewer tokens and faster” is too simple: after requiring the same restricted completed work, removal still saves input, but output increases and measured latency does not improve. Semantic errors remain in both arms.

The prespecified practically interesting threshold was a 10% cost or latency reduction **without loss of completion or factual quality**. The full result does not meet that combined criterion. Exploratory 95% bootstrap intervals, clustering repetitions by the eight cases, were −64.8% to +15.0% for input-token change, −34.2% to +15.7% for output-token change, and −51.3% to −2.5% for all-attempt elapsed time. They are wide, do not control server load, and do not turn faster incomplete work into equivalent performance.

## Where memory paid for itself, and where it did not

The following token totals use only complete-usage pairs for each case. They include the failed Bear attempt because its usage is known; completion qualifications must be read with the numbers.

| Case | Known-usage pairs | Input retained → removed | Output retained → removed | Qualification |
|---|---:|---:|---:|---|
| Junior — LULU | 2 | 78,168 → 72,806 | 3,970 → 5,979 | Both complete; removal −6.9% input but +50.6% output |
| Junior — STX | 2 | 67,162 → 13,116 | 3,972 → 2,022 | Removal skips the required note twice and misses a schema field once |
| Fundamental — LULU | 1 | 48,642 → 33,405 | 2,728 → 3,555 | 17/17 metrics on both sides; narrative and workflow defects remain |
| Quant — STX | 1 | 50,759 → 6,888 | 2,468 → 1,668 | 10/10 metrics on both sides; different substantive reasoning errors |
| Bull — LULU | 1 | 8,671 → 6,670 | 2,108 → 1,802 | Both complete; neither argument is semantically clean |
| Bear — LULU | 2 | 18,628 → 54,964 | 5,664 → 3,044 | Removal exhausts the turn budget once without an artifact |
| Regime — LULU | 1 | 7,644 → 5,193 | 741 → 778 | Both mislabel the same small positive DXY move as stable |
| Board — LULU | 2 | 73,817 → 46,333 | 9,956 → 9,319 | Four recoverable HOLDs; current-versus-historical reasoning differs |

A concrete retention benefit appeared in the second LULU Junior repetition: memory finished in four turns versus six, saving **10,869 input and 1,307 output tokens**, despite its larger prompt. It also surfaced relevant, explicitly attributed LULU verification questions. Across both LULU repetitions, however, retention costs 5,362 additional input tokens while saving 2,009 output tokens. If one output token costs more than approximately **2.67 input tokens**, retention wins that particular token-cost tradeoff; this run does not measure that hardware or billing ratio.

The Bear's second repetition also favors retaining something useful: with memory it produced an artifact in one turn; without memory it spent 47,244 input tokens on five tool turns and produced no artifact. The adverse fixture behavior described below limits the reliability inference, but the completed work and known cost cannot be omitted.

Conversely, the first memory-on Quant attempt spent at least **111,020 input tokens** across 21 whiteboard reads before its next request truncated, yielding no artifact. Retained past workflows included the same read pattern while the current prompt already said not to re-read supplied board context. That is a candidate conflict to investigate, not proof that one specific record caused the loop.

Input and output remain separate. The first-request memory delta multiplied by memory-on model turns estimates repeated memory input overhead; actual net work is measured from the full trajectories. The exported `memory_value_accounting` contains both quantities. Neither reported input usage nor that accounting estimate measures uncached GPU computation, and a bundle-level benefit does not establish that every constituent record is worth retaining.

## Output-quality findings

Manual review used the same source-grounding rubric on every attempt. It was not blinded, is not an independent quality score, and does not measure future returns. The exact-field checks alone miss material defects:

- **Cross-ticker date carryover:** the first memory-on LULU Bull assigned it an October 21 earnings date and “~10 weeks” phrasing found only in retained Ford memory, absent from the current role evidence. This did not recur in the second memory-on Bull run. The first removed-memory Bull failed transport; the second completed without that Ford date.
- **Stale candidate-list carryover:** the first memory-on Bear recommended GOOGL as if it were on the current candidate list. No list was supplied; GOOGL appeared in retained GS-session memory. The second memory-on Bear explicitly recognized and rejected that stale list. The first removed-memory Bear correctly returned no alternative; the second failed to finish.
- **Required work lost:** both removed-memory STX Junior runs skipped the note, and the second omitted the required `data_gaps` field. Gaps written elsewhere in prose do not satisfy the schema.
- **Removal did not solve hallucinations:** both removed-memory Quant answers invented dated chart paths without OHLC evidence. Bull/Bear answers in both arms contained incorrect risk/reward or downside arithmetic. Shared historical growth and reversal figures were sometimes presented as current. Both completed Regime counterparts returned the same incorrect direction label for the supplied +0.10% DXY change; this is a small directional-label mismatch, not an invented level.
- **Current-versus-historical context remains a problem:** some Board outputs called an older target current analyst consensus or treated different earnings periods as contradictions. Other outputs wrongly described successful empty whiteboard reads as unavailable tools. These are separate from raw JSON and parser acceptance.

Role visibility matters. Junior did not receive the authoritative Fundamental/Quant blocks available to downstream roles; it was not penalized for failing to copy facts it never saw. Its shared briefing still contains historical summaries and old lessons in both arms. Therefore this tests removal of upstream system-memory blocks, not removal of every historical assertion or every earlier learning change.

## Sequential component probe

The component probe ran only after the primary role cohort finished. It isolated the actual memory text with the same fixed eight-token diagnostic JSON, no tools and a 64-token cap. All **14/14 scored calls per arm** returned the requested JSON with complete usage. Median elapsed time was **2.36s retained / 0.53s removed**; median first content was 2.24s / 0.41s. Shared load/cache remained uncontrolled. This demonstrates a smaller diagnostic prompt and less observed waiting, not a 77% faster trading pipeline.

| Memory source role | Removed characters | Input retained → removed per request | Memory input overhead |
|---|---:|---:|---:|
| Junior Analyst | 6,452 | 1,599 → 70 | 1,529 |
| Fundamental Analyst | 6,479 | 1,616 → 70 | 1,546 |
| Quant Analyst | 10,772 | 2,927 → 70 | 2,857 |
| Bull | 8,627 | 2,071 → 70 | 2,001 |
| Bear | 6,713 | 1,664 → 70 | 1,594 |
| Regime Engine | 10,332 | 2,521 → 70 | 2,451 |
| Board | 5,690 | 1,417 → 71 | 1,346 |

One request for each role adds 13,324 input tokens with these bundles. That is not a per-cycle total: real routing, multiple turns, additional stages and retries vary. A diagnostic with only 70 baseline input tokens exaggerates percentage savings relative to a complete role prompt; the absolute overhead is the useful quantity.

## Protocol and limits

Actual role-scoped `<agent-memory>`, `<past-workflows>` and `<project-skills>` blocks were captured through supported external conversation GET APIs. The deployed lazy-agent-service filter produced the paired messages, with an equality assertion that those blocks were the only message difference. Current compact retrieval queries, reviewed skills, evidence, schemas and generation settings were identical. Prism was neither edited nor written to.

The real `run_v3_agent` assembler supplied the role prompts with external inference replaced during capture and database tripwires enabled. The model was GLM-5.3-Flash-EXL3, temperature 0, min_p 0, thinking disabled. The primary run used SSE streaming, an 8,192-token response cap and a 900-second total role deadline. Turn budgets were Junior 7, Fundamental 12, Quant 14, and Bull/Bear/Regime/Board 5. The Board persona was CONTRADICTORY / Jane Street. Calls were submitted sequentially in AB/BA order, reversed in repetition two. There were no adaptive response repairs or candidate changes during scored inference.

This is an **adverse frozen-evidence replay**, not a healthy full production harness:

- Missing recorded tool results produce explicit offline-unavailable errors. Required annotations cannot succeed against the empty board/limited fixture. Repeated Fundamental writes and other workflow deviations are manually reviewed; the usable-artifact-plus-Junior-note metric does not certify all-role workflow compliance.
- The frozen market-data adapter is keyed by case and ignores requested ticker. Two peer requests, DKS and CROX in the second removed-memory Bear attempt, received clearly labeled LULU reports. That trajectory's turn-cap failure is recorded unchanged, with the mismatch exposed in the public measurements; it cannot establish normal live reliability.
- Shared model traffic and prefix cache were uncontrolled. First-output timing includes content, reasoning or tool deltas and is recorded only for completed turns; it is not server-only TTFT. Failed requests' unreported tokens and any server-side work after truncation are unknown.
- Eight cases across two tickers, repeated twice, are not 16 independent held-out tasks. No production repair loop, reconciliation, order execution or P&L was tested.
- After the first Board HOLD response, an overbroad size probe was corrected for applicability. Its original numeric result is preserved, but the 0.6% headroom ceiling is evaluated only for BUY orders. Retaining the existing holding is not an oversize purchase. All four Board actions were HOLD, so BUY sizing remains untested.

Earlier runs remain separate: an invalid dependency setup run; seven completed buffered-transport attempts plus one administratively censored in-flight attempt; and an initial component probe whose last two roles overlapped the invalid setup run. The initial probe had one timeout, with unknown usage. Completed buffered observations sometimes favored retention, including fewer LULU Junior input/output tokens and fewer Fundamental input/output tokens with equal 17/17 fact accuracy. Those observations are preserved as exploratory evidence, not pooled with the corrected streaming cohort. The earlier skill-changing replay did not contain actual upstream memory and cannot answer this isolated ablation.

## Decision and artifacts

The test establishes a real repeated input burden and observed savings from filtering it. It **does not establish lower failure rates, better semantic quality or a production speed improvement**. Retention sometimes prevented extra work or omitted steps, while stale records sometimes contaminated decisions. Restoring the whole bundle would reintroduce demonstrated hazards; treating blanket removal as a completed quality improvement would ignore the regressions.

The next admission test should evaluate small, scoped candidate memories against their repeated input cost on independent tasks with complete tool fixtures, including adverse cases. Required-note and final-artifact completion need dependable harness handling. Current risk/output contracts should remain authoritative, and historical ticker facts must not become current policy or current evidence merely because they were retrieved. The companion [memory and rule audit](memory-and-rule-audit-2026-09-07.md) describes the admission, retrieval and retirement design.

The [complete sanitized result export](benchmarks/memory-isolation-role-replay-20260907.json) contains final summaries, every scored measurement and warmup, completed-turn usage/timing/load, schema-error counts, original sizing probes and applicability, fixture-mismatch metadata, frozen-artifact hashes, and separate exploratory cohorts. It excludes private prompts, source documents, tool arguments/results and generated research. The [original component export](benchmarks/memory-isolation-20260907.json) remains an explicitly exploratory historical artifact.

Runners, SSE handling, scorers and analyzers are in `scripts/benchmarks/` and `tests/unit/test_memory_benchmark_score.py`. The replay runner intentionally preserves the measured fixture behavior for reproducibility; do not use it as a normal-production reliability test. Private frozen inputs and all 32 manual reviews remain in the local benchmark corpus. Initialize Linux nvm and set `REPLAY_NODE` to its Node executable before an explicitly authorized rerun. Offline scoring uses `MEMORY_BENCHMARK_SCORE=1`; analysis and export make no inference calls. Capture provenance: trading-service `7eb4fad1bcd18542bdd7c9892a575573d5338410`, lazy-agent-service `7908bc5c8f4549d881b4dcfda861f4533f018416`.
