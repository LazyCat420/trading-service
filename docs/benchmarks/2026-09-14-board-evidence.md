# Board evidence delivery: first investigation

The Board's governing regime directive was missing from the initial model input in all 35 inspected cases, despite being present in the retained source artifact before invocation. The harness put it at the end of a research summary that was then truncated. The Board's system prompt explicitly gives that directive precedence over its persona. This is a delivery defect, not evidence that the Board ignored a directive it received.

## Completed change

The Board receives the complete `regime_classification` artifact in its own protected user-context block. Its compressed research summary excludes the duplicate regime rendering. Other agents retain their existing summary behavior. Missing regime evidence stays explicitly unknown. A `board.regime_delivery` trace records completeness, source hash, character count and whether the directive exists. Decision fields, confidence, financial validation, model discovery and retry budgets are unchanged.

The packet preserves rationale, factors, directive, uncertainty and source metadata; it does not replace them with an LLM summary. This costs a median 4,009 additional characters in the inspected cases. It is a correctness repair, not a token-saving claim.

## Historical delivery audit

Latest 60 Board telemetry rows yielded 35 distinct cycle/ticker cases with retained traces. Only the first invocation is counted. Whiteboard versions and annotations are restricted to timestamps at or before that invocation. This includes canary cases and is not a random production sample.

| Measure | Result |
| --- | ---: |
| Truncated initial research summaries | 35 / 35 |
| Available regime directives delivered initially | 0 / 35 |
| Complete directives delivered by the new renderer | 35 / 35 |
| Median initial user-input characters | 58,674 |
| Identical repeated whiteboard reads within captured first invocations | 2 |
| Whiteboard reads / annotations / summary requests | 80 / 22 / 6 |

Six cases later requested the regime section. Nine reads requested `final_decision` during the Board's own invocation; test whether role clarification removes those reads before changing tool permissions.

Tool counts deduplicate call IDs across cumulative provider payloads. They describe captured calls, not guaranteed complete server-side executions. Exact long-line repetition has a median of zero: semantic repetition still needs claim/source analysis. Large inputs alone do not prove attention failure. An annotation or a summary call is not inherently wasted work.

## Four-arm exploratory model replay

One frozen NVDA case compared original input, prompt guidance only, regime delivery only, and both. The endpoint's model identity and capabilities were discovered immediately before the batch. No live tools, paper orders or repairs ran. Frozen replies were reused; unknown requests invalidated the arm rather than inventing results.

The comparison is **inconclusive**:

- Original: stopped on an unrecorded annotation, after four provider requests.
- Prompt only: returned non-native tool-call markup and no parsed artifact. The saved initial report labels this `returned`; the final replay script distinguishes `tool_protocol_unhandled`. This is not successful completion or token savings.
- Delivery only: returned a parsed artifact with an invented financial reasoning reference, failing the financial contract. There was no correction pass in this experiment.
- Combined: stopped on an unrecorded portfolio request after two provider requests.

The prompt rewrite therefore remains only an experimental fixture. It is not enabled in production. The replay does not establish comparative quality, latency, token savings, or that another agent should be removed. The deterministic delivery fix does not depend on selecting a winner from these invalid/incomplete comparisons.

## Next Board experiments

1. Complete frozen fixtures for portfolio/risk reads and simulate annotation state explicitly, so a novel annotation does not invalidate a whole loop. Reproduce the deployed protocol path; do not silently convert arbitrary text into tool execution.
2. Compare the same four arms over held positions, prospective entries, unresolved dissent, stale data and missing evidence. Score required evidence components, unsupported references, decision/timing consistency and repair regressions before cost. Include failed attempts in cost accounting.
3. Trace each decision-bearing claim to its originating source. Several agents repeating one vendor claim must count as one source, not independent corroboration. Remove a context block only after an ablation shows material evidence retention and quality survive.
4. Inspect which research/debate fields are omitted by summary truncation versus recovered through tools, then test a section delivery manifest and targeted retrieval. Preserve completed answers and dissent rather than simply reducing the character cap.
5. Once Board quality is established, compare Board-only finalization against Board plus synthesis with equal responsibilities. Consensus-driven sizing and audit provenance must not disappear when an agent is removed.

## Evidence and reproduction

- `evidence/board-evidence-2026-09-14/delivery-audit.json`: per-case source receipts and measured delivery/count data.
- `evidence/board-evidence-2026-09-14/exploratory-replay.json`: exploratory responses, all measured usage and explicit fixture gaps. It is not an accepted performance benchmark.
- `scripts/benchmarks/board_delivery_audit.py`: reproduces the historical audit from frozen exports.
- `scripts/benchmarks/board_loop_replay.py`: offline-tool replay with fresh model discovery; accepts frozen trace and snapshot exports, guidance, endpoint box and tickers. `--regime-module` supports an isolated pre-deployment renderer without editing the running app.
- `tests/benchmarks/fixtures/board_evidence_workflow_candidate.txt`: unpromoted prompt candidate.

Validation: 44 focused SharedDesk/Board delivery tests passed; 13 Board delivery/replay isolation tests passed. The broader unit suite passed: 7,826 passed, 115 skipped (361.77 seconds); cross-repository schema publication/drift/parity checks were excluded because this change does not modify tool schemas. Deployment verification follows after integration.
