# Post-update decision quality audit — September 8, 2026

Status: IN PROGRESS. Work through the numbered checklist in order. Record failures,
fixes, validation and limitations; do not equate driver completion with agent success.
User scope: updates over the last several days, all agent roles, harness/tool changes,
tickets, memory and autoresearch; detect compounding bad evidence and noise.

## Checklist and required evidence

- [x] 1. Freeze inventory and baseline: repository/deployed commits, model and settings,
  enabled learning controls, active cycles, relevant September 5–8 changes. Preserve
  unrelated work. Map each change to a test and its previously measured limitations.
- [x] 2. Repair and validate transport: reproduce Quant's false stream stall, preserve
  signed tool scope and actual progress, verify real stalled/truncated streams still
  fail, retries remain bounded, cancellation releases resources, and failed costs are
  counted or explicitly unknown. Deploy owned adapter and verify HTTP plus transport.
- [x] 3. Verify paper execution: default/scheduled/watch/manual cycle flags, supported
  paper-only routing, decision intent, sizing and policy gates, order/fill attribution,
  position and cash consistency. Validate a normal trade-enabled cycle without forcing
  a BUY/SELL or bypassing portfolio rules. Observation-only runs are not execution tests.
- [x] 4. Audit tools and source evidence: all active role whitelists and schemas,
  identity/permissions, cache freshness and isolation, time scope, errors vs missing data,
  duplicate calls, idempotent writes and delivery receipts. Inspect actual recent calls.
- [x] 5. Audit every active agent: Junior, Fundamental, Quant, optional Valuation,
  Bull, Bear, Bull Defense, debate judge, Regime, Board and optional Synthesizer; verify
  actual enabled roles from code. Score artifact validity, grounded claims, arithmetic,
  timestamp/units, uncertainty, contradictions resolved, usable output, tool failures,
  retries, tokens and duration. Include all failures and distinguish DATA_GAP from error.
- [x] 6. Measure decision quality with paired frozen evidence: preregister cases and
  scoring before inference; include answerable/missing/stale/conflicting evidence and
  entry/exit/wait decisions. Compare current tickets against identical unassigned
  questions, then targeted memory/learning ablations. Hold model, evidence, tools and
  budgets fixed; counterbalance order, repeat, blind grading where practical. Score
  evidence correctness, decision justification, numerical/risk consistency, appropriate
  abstention and unresolved critical questions. Do not grade against desired trades.
- [x] 7. Audit memory for compounding noise: provenance and eligibility of live reads
  and writes, synthetic exclusion, cycle/ticker separation, duplicate/contradictory
  facts, expiry, consolidation failures, quarantine and downstream prompt receipts.
  Use isolated test stores for adversarial evidence; no benchmark writes to live memory.
- [x] 8. Audit autoresearch end to end: eligible outcomes and horizons, failed/empty
  cohorts, baseline/candidate comparisons, holdout leakage, repeated selection,
  promotion/rollback gates and independent controls. Test that unsupported candidates
  cannot become trusted guidance. Distinguish a functioning gate from a beneficial rule.
- [x] 9. Deployed validation of execution/harness fixes: complete trade-enabled normal analysis,
  inspect each stage's actual inputs/outputs and downstream effects, reconcile run/report/
  tool/cost/order records, verify no test contamination and clean owned test processes.
- [x] 10. Publish evidence matrix: helpful / harmful-fixed / unproven / unavailable per
  change and role, paired results with denominators and uncertainty, remaining failures,
  deployment health and follow-up measurement. Future paper returns require their
  actual observation horizon; never present immature outcomes as measured performance.

## Initial evidence and open defects

- Ticket pilot: 8/8 accepted answer records in both arms. Replacement cohort: 10/10
  tickets vs 5/10 controls accepted, but answer content 10/10 in both. Board reasoning
  issues 3/4 vs 4/4. All HOLD by observation instruction: investment quality unproven.
- Paper execution is enabled in the active normal ORCL cycle, cycle-v3-1788854618,
  observed trade_flag=true. Earlier observation cycle explicitly used trade=false.
- The adapter buffers tool arguments and emits empty strings. The provider only yields
  tool-argument progress for nonempty fragments, creating false 300s stalls even while
  the upstream generates. A deterministic regression reproduces this at exactly 300s.
- Do not modify or deploy Prism. Adapt supported interfaces in lazy-agent-service.

## Execution log

2026-09-08: checklist created; inventory and transport investigation underway.

## Inventory checkpoint (08:41 UTC)

Trading runtime c9ddbffc (repository d48e62f4 adds benchmark documentation); owned
adapter updated from 5736438 to f223a4a. The model cohort includes GLM-5.3-Flash-EXL3
and older nemotron35 attempts, so aggregate production comparisons are observational.
All seven active skill texts exactly match reviewed baseline version 2026090701.
Skills/indexing/consolidation enabled; generated proposals disabled; canonical serving 0.
Delta is active; Portfolio Manager has no observed role attempts and is not a panel role.
Contradiction shadow is deterministic telemetry, not a model agent. Zero-token regime
records may be cached classifications; do not automatically classify them as lost cost.

| Change family | Evidence already available | Remaining acceptance check |
|---|---|---|
| Signed tool scope, fresh whiteboard, retired peer tool | Prior transport/dispatch tests and live receipts | Current per-role routes, stale/cache/identity adversaries |
| Research questions and answer delivery | Four paired workflows, plus interrupted pilot | Held-out decision reasoning and contradictory evidence |
| Reviewed methods and upstream memory boundary | Earlier replay failed performance gate; filter canary passed | Isolated current-contract ablations and contamination probes |
| Synthetic memory exclusion | 41 focused tests plus live writes/reads | Current persisted cohort and every downstream learning reader |
| Intent, attribution and dissent contracts | Contract regressions | Non-HOLD decisions and paper gate consequences |
| Stream progress, retries and telemetry | New false-stall reproduction; bounded retries | Completed post-deploy Quant and honest partial-cost reporting |
| Outcome horizon, autoresearch, watch scheduling | Prior audits identify stale cohorts and unfinished checks | Current eligible cohorts, promotion gates, queue/trigger/book audit |

## Transport checkpoint

The deterministic progress test failed against the old adapter at 300 seconds and
passes with the fix. Actual Prism parser compatibility: six progress events and one
correctly authorized tool call. No Prism code or data was changed. Focused transport
suite: 66 passed; TypeScript check passed; full deployment gate: 671 passed. Trading
retry/partial-cost tests: 34 passed. Counts overlap and are not additive coverage.

Targeted deploy-kit completed transfer and restart at 08:39:52 UTC; adapter HTTP
health returned 200 and Docker reported healthy. Runtime image SHA-256 begins
5447ab26e3dd; deployment provenance f223a4a. Earlier ORCL was stopped at 08:35:59
with zero orders and a preserved cancellation row. Watch Desk started RH before the
restart; its model stages began afterward. This is mixed-cycle observational evidence,
not the clean full rerun required in step 9. Partial cost remains a lower bound where
provider usage is unavailable; transport success does not imply complete cost capture.

The three-day snapshot contains 2,694 resolved outcomes, none with exit_date. The
current main resolver now stamps a fixed horizon for new resolutions; legacy rows and
the challenger reader/resolver remain an explicit step-8 audit target.

## Paper and tool checkpoints

Paper component audit passed 85 focused tests. Read-only live reconciliation: 27
positions agree with remaining quantities across 62 lots; all 76 fills link to their
orders with matching bot/ticker/side; no orphan lots or invalid lot quantities. Cash
reconstruction for the three books with fills agrees within 0.000000000062 dollars.
The first scratch auditor used the wrong order key (`order_id` instead of stored
`id`); it was corrected against the actual schema before reporting results. No live
ledger rows were altered. End-to-end post-fix decision execution remains step 9.

Tool component suite passed 87 tests: scope, catalogs, whitelist enforcement, delivery,
collaboration, degraded research and decision contracts. Live MCP exposes 90 tools,
retired peer dispatch is absent, whiteboard description matches note semantics, and
a signed forbidden no-op is refused. Three-day production census: 1,443 tool calls,
197 recorded failures. This spans older deployments and multiple models; it is not
an after-only failure rate. Repeated argument hashes are diagnostic: fresh whiteboard
reads and justified retries are not automatically noise. Recent observed failures
include screener bridge timeout, news timeout, expired sessions around deployment,
and reserved-section writes correctly denied. Error labeling and downstream handling
remain in the evidence/role review. Whiteboard writes still lack general operation-ID
idempotency; timeout does not prove a write failed (existing characterization retained).

## Outcome evidence defect and correction

The audit found a material feedback risk: legacy labels lacked exit provenance,
entry and exit vendors could differ, held HOLDs and conditional entries could be
misgraded as flat forecasts, and early paper exits could rewrite reference outcomes.
New outcome-contract-v2 eligibility rejects those records. It pins a dated source,
uses each saved decision timestamp and a bounded seven-calendar-day reference,
and leaves unsupported claims ungraded. Challenger labels require matching reference
pairs; cycle-only execution traces no longer inherit an arbitrary ticker outcome.
Unvalidated tool-playbook advice is removed from production prompt assembly.

Focused validation: 95 passed, including isolated real Mongo positive/negative cohort
checks and learning lifecycle tests (no skips in that run). A decision-timestamp
regression and full unit validation follow. This repairs evidence integrity; it does
not establish better market predictions. Historical metrics based on the excluded
cohort must not be used as evidence of improvement. The active challenger experiment
is disabled; its historical success-only attempt denominator remains a limitation.

RH post-stream-fix observation completed at 09:32 UTC: 11/11 model-role attempts
SUCCESS, no partial-cost flags in those rows, Quant 1 turn. The final BUY was
conditional (enter_on_condition), not an immediate purchase; summary reports zero
trade attempts/fills. The narrative explicitly cited four legacy HOLD_MISS rows and
a historical calibration bucket, confirming that the tainted cohort reached decision
reasoning before the new eligibility deployment. Also inspect its proposed trigger
$159.90 / stop $143.70 / target $178.06: implied reward/risk is approximately 1.12,
below its own stated 2:1 floor. A structurally valid artifact is not an error-free
investment rationale. This observation does not pass decision-quality acceptance.

Validated runtime fix committed as 2e61169f and deployed with successful transfer,
restart, Docker healthy and HTTP 200 at approximately 10:06 UTC. The running code
reports zero verified outcomes: historical rows remain stored but are ineligible.
Dashboard follow-up 4272db6b applies that same rule to HOLD stats and pending counts;
24 related tests passed, including the actual isolated-Mongo dashboard result.
Full unit run: 6,955 passed / 10 failed / 97 skipped; after correcting stale fixture
expectations and import-time logging, affected suites passed (1,279 passes on the
first repair run; its final two failures subsequently passed in a seven-test rerun).
Last runtime edits independently passed 45 focused tests; no market-quality claim
is inferred from these software test counts. Cohorts overlap and counts are not additive.

The RH source audit also reproduced a tool-formatting error: get_market_data rendered
stored short_float_pct=0.5306 as `ShortFloat%: 0.53`, while the verified fundamental
block correctly said 53.06%. Junior interpreted a 100× conflict; Fundamental and Quant
reconciled it. e54b687b renders normalized profit margin, ROE, revenue growth and short
float in explicitly labeled percentage points. Seven focused checks passed. This is
an observed reduction in contradictory inputs, not yet a measured reasoning gain.

The final replay dry run passed all 46 workflow instances (78 role invocations) with
no model inference. An initial dry invocation used the wrong disposable database-name
prefix and was refused before inference; corrected configuration passed. Scored
cohort remains unstarted at this checkpoint. Independent frozen inputs include four
cases covering headroom, missing history, held deterioration and conditional entry.

Memory lineage audit (10:14 UTC): 106 currently eligible canonical memories have
208 source links; all match archived quotations and ticker scope, with no synthetic
sources or missing archives in this cohort. This verifies lineage, not market truth.
There are 15 repeated quote groups (21 repeated links), so quotation count is not an
independent-evidence count. Canonical serving stays off. Consolidation jobs: 25
successful, six empty, one PLTR job held for review after three timeouts. Sixteen
incident records are indexed candidates and unverified; zero skill candidates have
been promoted. All seven active skill texts match reviewed baseline 2026090701.
The dormant working-memory addendum has no active call sites; generic dense/hybrid
retrieval applies learned-content eligibility, and decomposed recall uses deterministic
facets instead of another model-generated query. Its usefulness still needs measured
retrieval relevance, not just successful execution.

Final combined unit suite: **6,967 passed, 97 skipped**, no failures, 387.65 seconds.
This supersedes the earlier failing full-suite checkpoint. The relevant real-Mongo
outcome, dashboard and learning-lifecycle paths were separately exercised successfully
with explicit isolated databases; skipped optional/live tests are not claimed passed.

Gateway audit after the stream fix found 41 payload receipts, all reporting upstream
facts/workflows excluded. Receipt application_state remains `unknown`: payload
filtering is delivery evidence, not evidence that the agent applied a method or made
a better decision. RH had 11 role delivery receipts, one for each model role. Its
successful role telemetry coexisted with warning/error logs and the quality defects
above, so completion, tool reliability and reasoning quality stay separate metrics.

RH inner-error audit: Bear and Bull Defense each logged one empty-response incident
before their terminal artifacts succeeded. These are recoveries, not extra successful
role attempts. Drift/no-research invariant alerts are incident telemetry; they do not
prove that a changed action mix or reusing supplied evidence was wrong. Autoresearch
reflection writes unverified candidates; generated skill serving is still frozen.

Final deployment 164e17d7 completed at approximately 10:26 UTC: transferred image,
restarted container, Docker healthy, HTTP health 200. Runtime assertion confirms
0.5306 renders as ShortFloat% 53.06 and verified outcome count is zero. Fresh normal
MSFT cycle cycle-v3-1788863242 started at 10:27:22 UTC via supported command queue,
trade_flag=true, full collection/analysis requested. No decision/action was forced.

Final harness integration suite passed 11 tests against an isolated real Mongo store:
research ownership/leases, answer evidence and delivery, allocator completion gates,
context manifests, stale writes, outbox recovery and synthetic isolation.
RH artifact receipts separate another denominator: 3/11 were eligible original outputs;
8/11 were delivered to the validation ledger as ineligible (`degraded_or_repaired`).
They were not promoted merely because terminal role status was SUCCESS. Joining the
11 receipts by conversationId finds 38 gateway payload receipts, all excluding upstream
facts/workflows. These receipts establish the boundary, not semantic correctness.

## Three-day observational role baseline

Snapshot ends 2026-09-08 08:41 UTC. These counts mix deployments, models and ticker difficulty; they are not a controlled before/after result. Cached Regime results and deterministic contradiction telemetry are distinct from model inference.

| Role | Attempts | SUCCESS | Other terminal outcomes | Partial-cost rows |
|---|---:|---:|---|---:|
| v3_bear_agent | 33 | 30 | CANCELLED: 1, AGENT_ERROR: 1, TIMED_OUT: 1 | 3 |
| v3_board_of_directors | 35 | 32 | AGENT_ERROR: 3 | 1 |
| v3_bull_agent | 36 | 32 | AGENT_ERROR: 3, TIMED_OUT: 1 | 3 |
| v3_bull_defense | 30 | 30 | none | 0 |
| v3_debate_judge | 31 | 30 | AGENT_ERROR: 1 | 1 |
| v3_decision_synthesizer | 39 | 37 | TIMED_OUT: 1, AGENT_ERROR: 1 | 1 |
| v3_delta_analyst | 2 | 2 | none | 0 |
| v3_fundamental_analyst | 36 | 33 | TIMED_OUT: 3 | 2 |
| v3_junior_analyst | 41 | 40 | AGENT_ERROR: 1 | 0 |
| v3_quant_analyst | 37 | 35 | TIMED_OUT: 1, CANCELLED: 1 | 2 |
| v3_regime_engine | 40 | 40 | none | 0 |
| v3_valuation_analyst | 31 | 31 | none | 0 |

Memory/autoresearch control audits are complete; method utility remains an open
benchmark question under steps 5–6. The source archive check covered all 106 eligible
canonical records and all 208 evidence links: no missing archive, quote mismatch,
ticker mismatch or synthetic source. It found 15 repeated-quote groups / 21 duplicate
links, which must not count as independent corroboration. Canonical serving stays off.
Generated candidates remain unverified and unpromoted; historical challenger failure
cost denominators are incomplete, and no matured verified outcome cohort exists.
These limitations preclude a measured learning-benefit claim.

Pre-inference calculator amendment committed as 431928e2 (benchmark only). All 46
dry workflows passed in 55.30 seconds; 30 source hashes frozen in a fresh scored
output directory. Production Quant exposes the core `evaluate_expression` tool
despite Trading's role whitelist; the replay now includes its actual supported
compute interface. Fresh MSFT telemetry already records two operand-format errors
(expressions supplied instead of numeric operands). Quant nevertheless completed;
its proposed 2.5×ATR stop and stated 2.48 reward/risk agree with its cited values.
This is arithmetic consistency with supplied inputs, not independent market truth.

Fresh MSFT supported conversation inspection confirms malformed tool requests: in
five of the first seven completed model roles, `<tool_call>` requests were embedded
inside `think` arguments. The executed tool was `think`, returning an acknowledgment;
Junior's intended news request and Fundamental's two intended screener requests did
not execute. Quant/Valuation/Bull also had embedded requests, with some later real
retries. This is a live harness/model interaction defect and missing research, not
an upstream data provider returning an empty result. Current replay replaces this
harness and cannot establish that this production defect is solved.

The stored Prism conversation includes pre-filter upstream memory; this alone is
not a leakage finding. Joining the first seven role conversation IDs to 28 gateway
receipts confirms agent-memory/past-workflows removed on every request. Fundamental's
four requests each removed 6,209 characters. The earlier suspected gap was ruled out
at the owned final-input boundary; no Prism change was made.

Live reasoning review found an actual propagation case: Bear called cash moving from
$94.6B to $75.5B a 25% decline; the cited operands give -20.19%. Defense repeated
-25% multiple times. Junior's invalid “4×” change from 0.0 to 0.4 also reappeared in
Defense. Defense's grounding shadow reported checked=4, mismatched=0, unverifiable=0;
those limited scalar checks did not catch the propagated narrative arithmetic. Its
original tool loop reached four iterations without a final artifact after repeated
whiteboard reads and two unsuccessful offload lookups, then the role recovered through
the bounded repair path. Terminal SUCCESS and quality_score=84 do not establish that
this artifact is fully grounded. These are current limitations, not fixed findings.

MSFT cycle completed at 11:39:51 UTC after 4,349 seconds: 11 model roles SUCCESS,
plus deterministic contradiction check. Reported total 1,562,216 tokens, including
1,453,657 input and zero reported cached tokens. All 46 matched model-input receipts
exclude upstream facts/workflows. Five original artifacts were eligible; six were
ineligible under the existing degraded/repaired gate. Six of 11 model roles had
embedded intended tool calls inside `think`. Defense and Judge needed bounded
artifact repairs. The final synthesizer reintroduced cash -25% and old 3.09 R:R
into the rationale while preserving the Board's widened stop and trade contract.

The normal paper attempt was BUY 72, enter_now, 1.5% with monitor-purpose RSI 30,
stop 470.13 / target 572.92. Policy allowed BUY, but execution refused the date-only
quote as 108h old (limit 96h). No order/fill was created. Seven books, 27 positions,
62 lots, 76 fills and 76 orders still reconcile, with cash deltas below 7e-11.

New concrete fix d697d29e preserves Yahoo's observed quote timestamp only when
symbol, market-local session date and price match the daily bar. Same-date daily
bars refresh with that provenance and an older response cannot regress it. Paper
execution uses the verified timestamp and persists reference-price source/age in
fill rows; the 96h limit remains unchanged. 63 focused unit checks passed, then 62
execution checks passed (overlapping suites); an isolated real-Mongo test verifies
refresh, anti-regression, actual fill/order/lot/position/cash and duplicate refusal.
Targeted NAS deployment completed at approximately 12:09 UTC, Docker healthy,
HTTP 200. Deployed collection refreshed five MSFT bars; the unchanged close moved
from 108.19h age to 88.19h using provider time 2026-09-04T20:00:01Z. The historical
rejected attempt was not replayed or relabeled as executed. Production price data
was refreshed through its normal collector, not manually backdated or made current.

The 78-role replay began after MSFT completed, on frozen sources at 431928e2.
The price-only runtime change affects neither the frozen prompts nor the isolated
replay loop; its 30 source hashes still match. A later normal cycle's availability
probe timed out while sharing the model server, reinforcing the uncontrolled-load
limitation. All scored attempts remain retained. Grading found corpus wording
“shares declined 2 percent” ambiguous between price and count; neither interpretation
is penalized in any arm. This does not alter prompts or the preregistered checks.

## Replay review and isolation checkpoint (12:39 UTC)

Thirty-five of 78 role artifacts have been manually reviewed under masked labels.
No scored source, prompt or corpus changes: all 30 source hashes still match.
The running replay is retained without selective retries. Conclusions about ticket
benefit and methods remain pending until all preregistered attempts are graded.

Conditional-entry agents generally distinguish current115/stop95/target130 R:R0.75
from future100/stop95/target130 R:R6.0, and reject price95 as an RSI threshold.
However, one Quant computes the $20 stop distance as5.7 ATR with ATR$3, and its
Board repeats this (correct6.67). Other observed propagation includes PEG compared
with peers' P/E, and an incorrect alternative-entry R:R. Some missing-history
outputs correctly reject all three unsupported facts yet serialize unavailable
metrics as zeros or invent a realized-drawdown proxy. These are separate from
contract acceptance and are retained with excerpts in the grading records.

Production trigger source inspection matters to semantic review: SMA triggers
compare price to the current moving average, ignoring the numeric `value`; RSI
triggers use the threshold. A Board calling `sma_50_drop value100` a $100 price
watch therefore describes different behavior from the deployed evaluator. It still
wakes re-analysis rather than executing a hypothetical fill directly.

Fresh memory audit:106 eligible memories,208 source links,zero source/archive/
ticker/synthetic mismatches;15 repeated-quote groups with21 duplicate links remain.
Canonical serving is disabled. All seven active methods match reviewed baselines.
Twenty generated records remain unverified candidates, zero skill candidates,
zero verified market outcomes. Four new candidates reference the completed MSFT
cycle, not the replay. PLTR remains review_required after three consolidation
timeouts; the newer MSFT consolidation completed empty.

A full nested-document marker scan of eight production collections found no EVLT
or scored-replay markers:1,007 observations,349 canonical memories,26 evidence
archives,20 learning records,97 artifact receipts,97 validation receipts,2,784
outcomes and183 research queue entries. This snapshot is during the replay; repeat
the isolation check when the run finishes. See the committed-safe memory evidence
JSON for counts and source hashes.

Source review explains two important limitations. First, the Quant prompt still
says “max drawdown ≈ 2×ATR floor” in its interpretation step, while its newer final
instruction requires copied realized trailing-year drawdown. The resulting proxy
errors are a conflicting system contract, not solely model invention. This frozen
source remains unchanged during scoring. Second, replay whiteboard dispatch calls
handlers directly after schema validation. Production performs ticker repair and
filters undeclared arguments in its SDK registry; replay TypeErrors for extra
`whiteboard_annotate(ticker=...)` arguments therefore do not establish production
failures. Both paired arms share the replay path, but transport failure rates are
not production estimates. The live MSFT audit supplies production evidence.

## Independent-role checkpoint (14:14 UTC)

All 48 paired handoff attempts and 18 independent-role attempts are recorded and
reviewed under masked labels. The 12 Board method/memory attempts remain in progress.
Every active model role has live or isolated content/failure evidence; both isolated
Judge cases lack artifacts, so those tests measure completion failure rather than
final reasoning quality. Missing-history Synthesizer preserves the Board contract
and the research gaps with no material error in the manual review.

The frozen Bear prompt itself contains the historical claim that 68% of HOLDs were
wrong and assumes a one-position book. Bear repeated this prompt-supplied statistic;
it is a static path outside dynamic outcome filtering. Corrections to that passage
and contradictory Quant measurement/estimate instructions are prepared outside the
scored source files and will be applied only after all 78 reviews are retained.

Replay timeout limitation: the first model invocation and tool-less repair each have
an outer 600-second wait. The replay mock does not honor the passed absolute deadline,
so the preregistered description of a strict 600-second total per role is imprecise.
Measured times and all failures remain in the results; no budget was changed mid-run.

## Scored audit complete (15:17 UTC)

All 78 attempts and all 46 workflows are retained. Driver: 46 passed in 12108.47s;
roles: 34 SUCCESS, 29 DATA_GAP, 15 AGENT_ERROR. All 78 manually reviewed; 63 usable,
10 fully evidence-consistent under the strict rubric. Tickets versus unassigned questions:
2/8 vs 4/8 clean Board decisions, 17/24 correct Board answers in both, 17/24 vs 11/24
machine-accepted answer records. Four cases remain the independent units. Method ablation:
current 1/4, omitted 0/4, stale memory 1/4; exploratory, not a promotion result.

All intended method/memory conditions matched recorded input delivery, and all 30 frozen
source hashes matched at the final pre-patch check (15:16:57 UTC). Every final synthetic
artifact is retained alongside its review hash/rationale. Eight replay-only extra-keyword
errors are explicitly distinguished from production behavior. All 113 numeric calculator
outputs match independent Decimal arithmetic; two requests used invalid arguments.

Post-scoring correction 86ba41d0 removes contradictory Quant measurement/estimate rules,
the zero drawdown template anchor, and Bear's unsupported HOLD statistic/one-position
assumption. The declared drawdown description now agrees with measured trailing-year
semantics. Focused regression validation: 181 passed. Deployment and source hashes verified; scored
results describe the original frozen prompts, not a measured improvement from this patch.


## Final deployment verification and incident

The runtime is healthy and all three deployed prompt/schema source hashes match
86ba41d0. The completed MSFT cycle validates preceding execution/harness fixes;
the final narrow prompt patch has 181 focused checks and deployed hash verification,
not a second full model cycle or a post-patch improvement measurement.

The rollout interrupted RBC cycle `cycle-v3-1788880665` at 15:27:40 UTC despite the
transfer preflight reporting BLOCKED. The deploy library ignored the hook's nonzero
return, and the separate restart path had no preflight hook. RBC had no trade attempts
or executions; Regime/Junior completed and Fundamental was cancelled. The scheduler
resumed with CRDO. No stopped history was rewritten and no RBC rerun was queued.
The corrected local workflow explicitly checks transfer hook returns and gates both
restart transports, including restart-only. Eighteen mocked scenarios, twelve existing
release-guard tests and twenty-four trading state/preflight tests pass. No additional
runtime restart was needed. Atomic scheduler quiescence remains future work.

Post-patch isolation scans find no synthetic markers across all eight live collections;
the three declared post-scoring edits are the only frozen-source differences. No audit
model run or deployment process remains active. Final evidence and role matrices are
in [the findings report](post-update-findings-2026-09-08.md).
