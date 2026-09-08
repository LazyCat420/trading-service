# Post-update decision audit — September 8, 2026

The updates improve specific reliability and containment controls, but this audit does
not demonstrate better overall decision quality. Research tickets increased machine-
accepted answer records without improving the Board's correct-answer count, and had
fewer fully evidence-consistent decisions in this small frozen comparison. Within-cycle
errors still propagate through peer review, including claims labeled “verified.”

Scoring is complete. Deployment of the final prompt-only corrections is pending verification.

## Paired decision results

Four independent adversarial cases, two repetitions each, identical questions/evidence,
GLM-5.3-Flash-EXL3. “Evidence-consistent” requires a usable artifact, no material
unsupported/wrong claim, and no applicable decision/risk violation. It is a strict
artifact criterion, not a claim that a trade will make money or that another action
is optimal. Correct answers include correctly identifying unavailable evidence.

| Measure | Unassigned questions | Research tickets |
|---|---:|---:|
| Usable Board artifacts | 7/8 | 6/8 |
| Evidence-consistent Board decisions | 4/8 | 2/8 |
| Explicit Board answers correct | 17/24 | 17/24 |
| Artifacts with material errors | 3/8 | 4/8 |
| Machine-accepted answers across handoffs | 11/24 | 17/24 |

Per-case clean decisions across the two repetitions: headroom 1–0, missing history
1–0, held deterioration 2–1, conditional entry 0–1 (questions–tickets). Three cases
favored unassigned questions; one favored tickets. This is an unfavorable signal for
tickets, not a precise estimate of their general effect. Six of the seven deferred
ticket answers were in the deliberately sparse-history case.

The separate exploratory Board comparison produced 1/4 evidence-consistent decisions
with reviewed methods, 0/4 with the reviewed prefix omitted, and 1/4 with methods plus
explicitly stale other-ticker memory. One output was missing in the omitted-prefix arm.
These four-case, single-repeat results establish neither stable method benefit nor
comprehensive resistance to memory contamination. Some stale claims were explicitly
rejected while unrelated reasoning errors remained.

## Every active model role

Fundamental, Quant and Board use the 16 paired handoffs; the other roles use two
independent cases with fixed peer artifacts. Different tasks and tiny samples prevent
a general ranking. Board ablation attempts are reported separately above.

| Role | Attempts | Usable | Explicit answers correct | Evidence-consistent |
|---|---:|---:|---:|---:|
| Fundamental | 16 | 11 | 33/48 | 1 |
| Quant | 16 | 16 | 44/48 | 0 |
| Board (paired handoffs) | 16 | 13 | 34/48 | 6 |
| Junior | 2 | 2 | 5/6 | 0 |
| Valuation | 2 | 1 | 3/6 | 0 |
| Bull | 2 | 1 | 3/6 | 0 |
| Bear | 2 | 1 | 3/6 | 0 |
| Defense | 2 | 1 | 3/6 | 0 |
| Judge | 2 | 0 | 0/6 | 0 |
| Regime | 2 | 2 | 5/6 | 0 |
| Synthesizer | 2 | 2 | 5/6 | 1 |
| Delta | 2 | 2 | 5/6 | 0 |

Across all cohorts: **63/78 usable artifacts; 10/78 evidence-consistent; 53 artifacts
with material errors; 15 missing artifacts**. Two Quant sizing recommendations breached
the supplied cap; no replay orders were executed. All 63 usable artifacts carried the
internal “good” quality label. Minimum schema acceptance and that label do not establish factual
correctness. Of 234 question opportunities, 173 were correct, 10 had a correct conclusion
with wrong support, 4 were incorrect, 2 were unanswered, and 45 lacked an artifact.

The live MSFT cycle independently completed all 11 model stages in 72.5 minutes:
Regime, Junior, Fundamental, Quant, Valuation, Bull, Bear, Defense, Judge, Board and
Synthesizer. Delta was covered by the isolated tests. Portfolio Manager was not an
active model panel role; contradiction shadow is deterministic telemetry.

| Live role | Useful behavior | Remaining issue |
|---|---|---|
| Regime | Separates low volatility from weak breadth | Zero breadth interpreted without proving input completeness |
| Junior | Separates catalyst dates and missing guidance | “Fourfold” increase from zero; intended news request never dispatched |
| Fundamental | Separates business and weekly horizons | Embedded peer requests mistaken for empty provider responses |
| Quant | Correct final stop and 2.48 reward/risk | Invalid calculator operands; unsourced chart anchors |
| Valuation | Consistent valuation arithmetic and distinctions | Embedded request recovered through genuine screener calls |
| Bull | Actual source searches and timing concession | Unsupported event-risk bound; embedded market request |
| Bear | Challenges source strength and timing | Cash decline called -25% instead of -20.19% |
| Defense | Makes concessions and downgrades weak evidence | Repeats cash/zero-base errors; artifact repair needed |
| Judge | Separates snapshot inputs from secondary claims | Calls wrong cash percentage verified; retains stale reward/risk; repair needed |
| Board | Compliant sizing and corrected reward/risk | Wrong SMA percentage and unsupported event certainty |
| Synthesizer | Preserves Board trade fields | Reintroduces wrong cash percentage and stale reward/risk |

The live error chain is concrete: Bear’s -25% cash claim passes through Defense,
is called verified by Judge, and reaches Synthesizer. Separately, Board uses the
updated 2.48 reward/risk, but Synthesizer restores the older 3.09. All **46 matched
final-input receipts** exclude upstream learned facts/workflows. This is within-cycle
propagation despite a working memory boundary, not evidence of an upstream memory leak.

## Evidence matrix

| Area | Finding | Assessment |
|---|---|---|
| Tool stream progress | Old false stall reproduced at 300 seconds; progress/parser and bounded-failure tests pass; owned adapter deployed | Specific reliability fault fixed; other recorded RuntimeErrors remain |
| Percentage units | Fraction 0.5306 had displayed as 0.53%; formatter now emits 53.06% | Concrete input fault fixed |
| Outcome provenance | Source/date/horizon and independent execution evidence required; eligible outcome cohort is zero | Containment validated; learned benefit unavailable |
| Static prompt anchors | Bear’s unsourced “68% of HOLDs” statistic bypassed dynamic outcome filtering; Quant instructed both historical drawdown and ATR substitution | Corrected after scoring; model behavior after the correction is unmeasured |
| Intent and execution | Delta keeps buys off its fast route; Synthesizer preserves Board contracts; conditional wakes re-analyze | Useful structural behavior; narrative/trigger semantics still fail |
| Research tickets | Accepted answer records 17 vs 11, correct Board answers 17 vs 17, clean Board decisions 2 vs 4 | Delivery improves in this replay; decision-quality benefit not shown |
| Reviewed methods | Exploratory clean counts 1/4 vs 0/4; stale-memory arm 1/4 | Benefit and broad contamination resistance unproven |
| Canonical memory | 107 eligible records, 210 source links, no lineage issues; 15 repeated-quotation groups | Lineage verified, usefulness unproven; serving remains off |
| Generated autoresearch | 23 indexed records remain unverified candidates; proposals disabled; no skill promotion; PLTR held after three timeouts | Promotion boundary works; no validated learned policy |
| Paper quote age | Date-only timestamp caused false 108h age; matched provider timestamp yielded 88h against unchanged 96h limit | Deployed provider refresh and isolated fill passed; original MSFT rejection preserved |

Paper books: seven bots, 27 positions, 62 lots, 76 fills and 76 orders reconcile with
no issues (maximum cash rounding difference 6.18e-11). The MSFT cycle requested trading
and attempted one BUY, but created **no fill** because it ran before the quote-age fix.
The subsequent real-provider refresh validated the deployed correction; successful
order/fill/lot/cash validation used an isolated database. No forced production trade
or mature post-update return is claimed.

The final case-insensitive isolation scan covers observations, canonical records,
evidence archives, learning candidates, artifact/validation receipts, outcomes and
research queues. It found no benchmark markers. Source verification is not independent
verification of market truth; repeated quotations and unverified candidates remain visible.

## Limits, costs and remaining work

- One reviewer, masked labels where practical; logs and artifact content can reveal arms.
  Four independent cases do not become eight independent cases through repetition.
- Replay uses a smaller tool budget and replaces the production provider/tool loop.
  Eight unexpected-keyword errors would be filtered by the production SDK. The replay
  omits the native `think` interaction and cannot show that its malformed embedded calls
  are fixed. Six of eleven live MSFT roles exhibited that interaction defect.
- The initial invocation and tool-less repair each have a 600-second outer wait; the
  mock does not enforce the passed absolute deadline. The protocol's strict total-role
  timeout description is imprecise. No budget/source changed during scored inference.
- All 78 role attempts' recorded method/memory conditions match the intended
  delivery. The stale-memory probe clearly labels old, unverified, other-ticker content.
- All 113 numeric calculator results match an independent Decimal calculation. Two
  requests failed on invalid arguments. This checks arithmetic execution, not operand
  choice, the meaning of a ratio, or claims the agent never sent to the calculator.
- Replay reported 1,983,208 input + 207,699 output tokens (2,190,907 known total).
  Three roles have incomplete usage; missing cost is unknown, not zero. Cache usage is
  unknown on 224 recorded turns. There were 304 tool calls and 14 tool errors, including
  the eight replay-only keyword errors. Shared GPU/cache activity prevents speed claims.
  The driver passed 46 workflows in 3h21m48s; this does not mean 78 agents succeeded.
- Live MSFT reported 1,562,216 tokens, including 1,453,657 input tokens. It is a separate
  operational observation, not a controlled cost comparison.

Next engineering priorities: make embedded intended tool calls fail visibly through
our owned adapter; verify important numeric/temporal claims at handoffs; align fixed-price,
SMA and trailing-trigger descriptions with monitor semantics; then repeat larger frozen
comparisons and observe properly matured paper outcomes. Do not promote learning on
schema scores or immature returns.

Validation completed before the final prompt patch: 6,967 unit tests passed, 97 skipped;
focused real-Mongo checks cover learning lifecycle/outcome eligibility and research
handoffs; harness integration passed 11 checks. The quote fix passed focused unit and
isolated order/fill tests. After the prompt patch, 181 focused tests passed. Counts
overlap and are not additive coverage. The full MSFT cycle predates the final narrow
prompt corrections; those corrections have focused validation, not a new scored model
cohort or a repeated full-cycle performance claim.

Runtime prompt correction commit: **86ba41d0**. Owned adapter: **f223a4a**.
NAS runtime verification: **running and healthy**, HTTP `/health` passed; hashes of all
three changed runtime files match 86ba41d0. Prism was not edited or deployed.

Deployment incident: the transfer preflight rejected active RBC cycle
`cycle-v3-1788880665`, but its caller ignored the failure and the separate restart
bypassed that hook. The container restarted at 15:27:51 UTC; RBC stopped during
Fundamental, after Regime and Junior completed, with zero trade attempts or executions.
The watch scheduler subsequently started CRDO. RBC's stopped history is preserved;
no manual rerun was queued. This was a deployment failure despite healthy runtime.

The local deployment workflow now explicitly rejects failed transfer hooks and invokes
a fail-closed `PRE_RESTART` hook for both transports and restart-only runs. Validation:
18 isolated scenarios, 12 release-guard tests and 24 trading preflight/state tests passed.
This workflow-only correction needs no container restart. A small race between the
snapshot and shutdown remains; fully closing it requires scheduler quiescence.
Post-patch scans again found zero benchmark markers in all eight live collections;
only the three declared post-scoring source files differ from the frozen manifest.

Evidence: [checklist and execution log](post-update-audit-2026-09-08.md),
[preregistered protocol](decision-quality-protocol-2026-09-08.md),
[scored results and review rationales](evidence/decision-quality-results-2026-09-08.json),
[final synthetic artifacts](evidence/decision-quality-artifacts-2026-09-08.json),
[live cycle and paper validation](evidence/post-update-live-validation-2026-09-08.json),
[memory lineage and isolation](evidence/post-update-memory-validation-2026-09-08.json).
