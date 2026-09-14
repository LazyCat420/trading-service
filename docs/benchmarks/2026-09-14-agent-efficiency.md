# Agent efficiency investigation — 2026-09-14

## Evidence and limits

Read-only NAS inspection of the latest 350 telemetry rows found 295 model-attributed runs across 14 cycles, September 11–14, after excluding the active cycle. Counts include retries and multiple tickers. Only 11 completed runs have complete attempt accounting; historical repair costs are incomplete. This mixes code versions, inputs, and workloads. These are triage statistics, not controlled model rankings or investment-quality scores. No runtime changes or model A/B benchmark were performed in this investigation.

| Agent | Runs | Median recorded tokens | Median seconds | Errors / data gaps | Complete usage runs |
|---|---:|---:|---:|---:|---:|
| debate_judge | 25 | 79,755 | 176.8 | 1 / 0 | 1 |
| board_of_directors | 30 | 79,658 | 121.1 | 2 / 0 | 1 |
| junior_analyst | 28 | 72,692 | 202.4 | 0 / 0 | 1 |
| bear_agent | 25 | 65,182 | 240.0 | 1 / 0 | 1 |
| fundamental_analyst | 29 | 58,375 | 207.1 | 1 / 0 | 1 |
| valuation_analyst | 28 | 57,392 | 142.2 | 0 / 0 | 1 |
| quant_analyst | 28 | 56,908 | 153.1 | 0 / 0 | 1 |
| bull_defense | 24 | 47,759 | 201.4 | 0 / 0 | 1 |
| decision_synthesizer | 40 | 46,528 | 115.3 | 6 / 1 | 1 |
| bull_agent | 25 | 40,690 | 157.2 | 1 / 1 | 1 |
| regime_engine | 13 | 6,772 | 53.5 | 0 / 0 | 1 |

## Priority findings

1. **Decision synthesizer: highest priority for reliability and context efficiency.** Six AGENT_ERROR rows and one DATA_GAP among 40 rows. Failures include missing artifacts, undeclared Board timing/trigger overrides, and corrections changing protected reasoning. Several belong to the same historical cycle, so this is not a 17.5% independent-cycle failure estimate. In the fully measured BHP canary, synthesis consumed 464,860 tokens (37.04% of cycle total), including 499 completion tokens. Its historical median is much lower: the canary is a costly observed case, not the typical cost.
2. **Debate judge and Board: highest historical median recorded token cost**, about 79,755 and 79,659 respectively. The canary judge needed a repair and Board a correction. Keep first-pass acceptance separate from eventual SUCCESS.
3. **Bear: highest median elapsed time**, 240 seconds. Fundamental research follows at 207.1 seconds and junior research at 202.4. All research tools and queue delays contribute; this does not prove weaker reasoning or slower model generation. Bull defense also warrants attention: the canary and the currently running cycle both show an initial response followed by repair.

## Why the synthesis case is expensive

Retained untruncated provider payload and tool-result snapshots show 18 whiteboard reads over 13 model requests. Requests reprocess growing conversation history: serialized messages grow from 72,561 to 194,872 characters; across requests they total 1,964,858 characters, 10.08 times the final request. This character ratio is a diagnostic, **not a tokenizer measurement or a predicted 90% token saving**. The 18 reads address distinct sections plus a final overview, so an identical-arguments cache alone would not solve this case. Several queried sections are tiny results; inspect actual absence status before treating them as missing.

The assembled prompt already carries financial evidence, precomputed math, portfolio and market context, previous-cycle material, compressed desk context, full Board verdict and debate structure. Compression requires fallback reads for omitted evidence. Removing read access or lowering the turn limit would risk recreating incomplete-artifact failures.

## Concrete optimization sequence

1. **Deliver one complete, versioned evidence packet to synthesis.** Assemble required current-cycle Board, judge, bull/bear/defense, research and risk records once. Include a manifest identifying complete, omitted and unavailable sections, with source IDs and versions. Fetch missing sections in a batch through a supported trading-owned interface; keep targeted reads for genuine gaps. Remove only exact duplicate representations after verifying references. Do not strip dissent or replace full evidence with a generated summary. This addresses repeated prefill work without reducing the available evidence. Changes belong in trading-service and, if tool compatibility is needed, lazy-agent-service; never Prism.
2. **Make contract compliance easier on the initial answer.** Give synthesis one authoritative output contract and an explicit Board field-preservation map. Preserve code-rendered financial step selection, action/confidence authorship, entry intent, trigger purpose and dissent resolution. Similar contract alignment should target judge/defense repairs. Do not silently fix decisions, relax validators, or expand retries.
3. **Use role-specific evidence views for judge, Board and research.** Pass full opposing claims and answer records to debate reviewers; supply relevant code-computed financial facts with source references. Avoid repeating the whole market briefing where the role already has the same facts. Retain explicit retrieval for uncopied sources. Benchmark each role separately; research cost may buy useful verification.
4. **Evaluate routing and caching after context fixes.** Discover the actual models and capabilities on every box at experiment startup. Compare roles on identical frozen inputs before selecting cheaper/faster endpoints. Stable prompt prefixes may improve provider cache reuse if supported. Cache deterministic computations by source hash and current-cycle/version identity; do not globally cache mutable whiteboard state. The canary reported no cached-token benefit, so none is assumed.

## Benchmark and promotion gate

Build paired baseline/candidate replays with frozen source and whiteboard snapshots, identical discovered model/provider and generation settings, alternating execution order. Start with at least 20 distinct fixtures and three repetitions per arm, covering BUY, SELL, HOLD, held positions, conditional entries, disagreement, missing evidence and historical contract failures. This is an initial engineering screen, not proof of statistical equivalence. Replay tools must be read-only and must reject unavailable fixture data rather than fetching a different live input.

Record total input/output tokens across every provider request and repair, coverage, latency, request count, first-pass validity, final validity and tool evidence coverage. Compare financial references and calculations, Board preservation or justified override, dissent responses, trigger semantics, unsupported claims and unknown-data handling. Review decision differences against evidence; mere action agreement or passing JSON is insufficient. Blind-review changed substantive answers. Report per-fixture paired differences and uncertainty, rather than pooling different inputs.

Promote only candidates with demonstrated token reduction and no observed regression on these checks; retain fallback behavior and monitor a no-trade canary. Use 20% total-token reduction as an experiment target, not a promised outcome. Any observed loss of evidence or decision-contract correctness blocks promotion. Keep live trading-cycle restarts out of benchmarking.

## Status

Investigation and optimization design complete. No savings or same-quality equivalence has yet been demonstrated by a controlled A/B run. The live cycle was running during inspection and was left undisturbed. Runtime deployment is not applicable to this documentation-only result. Aggregate measurements are in the companion JSON; raw snapshots remain in the existing NAS lineage store.
