# Trading reliability validation — September 14, 2026

The audited defects were corrected, integrated into the primary branches, pushed, and released through deploy-kit. End-to-end canary results are recorded below. This benchmark measures software reliability and measurement coverage; it does not establish investment merit or a statistically significant speed improvement.

| Deployed component | Source commit |
|---|---|
| trading-service | `abb9aa28` (master) |
| trading-client | `d7e72ecd` (main) |
| lazycat-sdk | `13d46b0` (main; synchronized into service) |
| scraper-service | wrapper `7673eb0`; staged trading-service source `abb9aa28` |

The previously deployed service baseline, including 98 commits absent from master, was integrated before release. No Prism repository changes were made. SHA-256 hashes of the loaded NAS SDK `agent.py` and `llm.py` match the validated primary checkout. All three containers passed HTTP health checks; service and client image labels and scraper `/health` source identity were checked independently of the deploy command.

## Correctness results

| Defect / acceptance condition | Result |
|---|---|
| Initial output 10 plus repair output 20 | Reports 30; unknown additional usage marks coverage partial; measured zero remains zero |
| Cancellation after cumulative usage updates 10 then 20 | Preserves 20 output and 120 total once; closes response |
| Requested provider versus misleading model-only SDK cache | Explicit discovered provider wins |
| Arbitrary model name, model swap, empty or ambiguous catalog | Discovers new ID/context; refuses stale or ambiguous identity |
| Twelve simultaneous forced discovery calls | One catalog request; all callers receive discovered identity |
| Model mismatch / unknown context | Refuses mismatched override; no borrowing another model's context window |
| Three failures on one news domain | Another domain remains eligible; cooldown probe and verified recovery reopen breaker |
| Concurrent news sweeps | Breaker state remains task-local |
| Quoted JSON delimiter strings and escapes | Authored string values survive repair |
| Five successive frontend chunk failures | One automatic reload per deployment; blocked storage preserves manual recovery |
| Production selector guard | Active agent/workflow call sites reject literal model selection |

Integrated service regression: **8,101 passed, 141 skipped**, 355.56 seconds. The final endpoint-default and evidence-label changes passed **29 targeted tests** after integration. SDK release transport/accounting suite: **17 passed** (earlier broader SDK validation also passed). Frontend production builds and chunk-recovery tests passed. Skipped tests retain their environmental/explicit-integration gates; the full suite is not a claim of exhaustive production database validation.

## Live measurements

Three fresh admission checks independently discovered both boxes and tested parsed tool calls plus structured JSON. All six endpoint receipts were eligible. Combined check times: **1.836, 1.671, 1.647 seconds**; median **1.671 seconds**. These small warm-system probes establish compatibility, not throughput capacity.

| Endpoint identity | Discovered model | Advertised context |
|---|---|---:|
| Jetson / vllm | nemotron35 | 128,000 |
| Gold Spark / vllm-2 | GLM-5.3-Flash-EXL3 | 850,000 |

The standalone deployed scraper also resolved its model dynamically through its own packaged discovery helper.

The toolless chat probe returned the requested JSON and served identity, with **27 prompt + 6 completion = 33 total tokens**, one measured request. Model names above are observations in this report, not production selectors.

The historical audit was recomputed without rewriting its source records:

| Retained cycle | Measured output | Measured model runs | Coverage |
|---|---:|---:|---|
| BHP `cycle-v3-1789360974` | 22,654 | 11/11 | Partial: historical repair cost unverified |
| NVDA `cycle-v3-1789359002` | 25,528 | 11/11 | Partial: historical repair cost unverified |

`A8_completion_tokens_recorded` now correctly reports true for both. The tool-count heuristic is named `A2_tool_call_pressure_agents`; it no longer claims measured iteration exhaustion. Historical total-token cost is labeled recorded, since old fallbacks may include estimates. Version 2 evidence keeps requested and served identities separate and attributes mixed-box retry cost by attempt.

A muted browser loaded the deployed dashboard with **zero runtime errors** and expanded BHP. Actual rendered text: “Output tokens: 22,654 · partial coverage · 11/11 model runs measured · Repairs recovered: 2/2”. The test browser closed afterward.

## Full cycle canary

Canary: `reliability-canary-20260914T175240Z`; command `reliability-benchmark-90e84304`. One explicit BHP ticker, full analysis, collection enabled, **trade=false**. The normal queue admitted it only after idle-state and pending-command checks.

The bear agent required a real WRONG_SHAPE repair. Its persisted attempt records contain **185 initial + 3,987 repair = 4,172 completion tokens**, two measured requests, complete coverage, and matching requested/served identities for both attempts. Total cost also reconciles: **63,204 + 18,094 = 81,298 tokens**. The initial malformed response remains visible even though repair recovered it.

Completed **done** in **28.74 minutes** (1,724,630 ms).

**11/11 model runs had complete output-usage coverage**, with **29,336 measured completion tokens** across **15 recorded attempts**. Every attempt's requested and served model/provider matched. Every persisted run total reconciled to its attempt records for both output and total tokens.

Artifact recoveries: **3/3**, plus one board correction. The successful bear, bull-defense and judge outcomes each included a repair; success is not first-pass acceptance. Decisions: BHP HOLD (confidence 72). No trade was attempted or executed.

Collection summary: 2 OK, 0 errors, 4 skipped, 0 late. These collector lifecycle counts are distinct from article-body coverage.

The saved cycle includes its fresh discovery/capability snapshot and deployed build identity. Summary/result counts reconcile. This is one full research cycle with trading disabled; the historical BHP run had different inputs and trigger context, so their wall times are not a controlled A/B comparison.

| Agent | Attempts | Measured output tokens | Coverage |
|---|---:|---:|---|

| v3_regime_engine | 1 | 1,294 | complete |

| v3_junior_analyst | 1 | 3,505 | complete |

| v3_fundamental_analyst | 1 | 3,633 | complete |

| v3_quant_analyst | 1 | 2,993 | complete |

| v3_valuation_analyst | 1 | 2,619 | complete |

| v3_bull_agent | 1 | 1,974 | complete |

| v3_bear_agent | 2 | 4,172 | complete |

| v3_bull_defense | 2 | 3,286 | complete |

| v3_debate_judge | 2 | 2,109 | complete |

| v3_board_of_directors | 2 | 3,252 | complete |

| v3_decision_synthesizer | 1 | 499 | complete |

## Next investigations supported by these results

1. Profile actual context delivery: the final decision consumed **464,860 total tokens** while producing **499 output tokens**, out of **1,254,924 total recorded tokens** across the 11 runs. Inspect report duplication, input size and cache effectiveness before reducing context; required evidence and dissent must survive.
2. Measure whether repaired artifacts preserve the underlying research and financial evidence, then compare matched inputs across roles/boxes. Successful parsing and lifecycle completion alone cannot rank decision quality.
3. Profile article-upgrade queues and deadlines. This canary recovered 8/12 bodies in one batch; a subsequent four requests exceeded the separate 20-second upgrade budget and retained summaries. Domain-breaker isolation does not remove that throughput constraint.
4. Trace duplicate market-data inputs behind the observed startup “Index contains duplicate entries, cannot reshape” warning; The warning follows `compute_all_correlations`; inspect sector/date and ticker/date input uniqueness before choosing a deduplication policy.
5. Validate mature outcome coverage, price cutoffs and horizon attribution before using confidence calibration or P&L to tune the strategy. One no-trade canary cannot settle that question.
