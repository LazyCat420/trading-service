# Whiteboard, write timeouts, and debate consolidation

Audit captured September 7, 2026, 20:52 PDT. Local tests use synthetic data and isolated stores. Production inspection was read-only aggregation of the trading database; no model requests, account operations, or Prism repository changes.

## Result

Keep **one shared workspace** for evidence, notes, questions, objections, answers, and decision records. Debate should be a structured review of those records. It is already partly implemented this way: the live Bull → Bear → Defense → Judge path publishes its artifacts to the same whiteboard used by the research agents. Removing the whiteboard or the adversarial review would remove useful functionality without addressing the actual duplication.

The current board supports asynchronous notes and next-agent handoffs. It is **not yet an actionable ticket system**. Posting a comment neither assigns an owner nor guarantees an answer. Reuse the existing research queue for deferred work; do not revive the retired peer-request dispatcher or introduce another scheduler.

## Timeout experiment

| Isolated experiment | Observed outcome |
|---|---|
| Real adapter deadline 60 ms; accepted HTTP write completes after 250 ms | Adapter reports `TOOL_TIMEOUT`; connection remains open; underlying write subsequently succeeds. |
| Real bridge fetch timeout 60 ms; accepted HTTP write completes after 250 ms | Fetch abort closes transport; server still performs the accepted operation. |
| Server immediately flushes headers; bridge timeout 60 ms; adapter 120 ms | Bridge clears its timer at headers. Body/write remain pending when adapter returns timeout; operation later succeeds. |
| Actual Python `Whiteboard.write_section`, isolated document store, subscriber blocked after persistence | Cancelling the Python task does not undo the stored entry. Retrying identical content creates version 2. |

These establish separate boundaries: stopping the caller's wait, aborting transport, and cancelling application execution are different operations. The HTTP fixture is not a production Mongo write test. The Python test exercises the actual whiteboard write ordering with database calls replaced by an isolated store.

**A timeout does not establish that a write failed.** The adapter already tells the model not to retry the same timed-out call. Reliable recovery still needs a stable operation ID and a status lookup; client abort alone cannot provide rollback. This cleanup does not add cancellation, idempotency, or rollback guarantees.

Reproduce: `npm test -- src/services/__tests__/WriteTimeoutAudit.test.ts` in lazy-agent-service (3 cases); `python -m pytest tests/unit/test_whiteboard_collaboration_audit.py -q` in trading-service (6 cases).

## What currently works

| Component | Actual responsibility | Remaining gap |
|---|---|---|
| `SharedDesk` | Typed artifacts, phase state, bounded downstream evidence, protected defense/verdict context | Artifact bodies also persist as whiteboard versions; publications are coordinated by several call sites. |
| Whiteboard | Versioned ticker/cycle sections and append-only comments | A comment is free text, with no assignee, acknowledgement, deadline, or resolution. |
| Whiteboard subscriber | Queues the next eligible role on artifact publication, with cycle filters and dispatch-once latches | Process-local, not a durable event outbox. Annotations intentionally do not rerun roles. |
| Agent runner | Reads a fresh board summary when each agent starts; retains unique notes and avoids repeating SharedDesk artifact bodies | Does not interrupt a busy model call. No per-note delivered/answered receipt; summaries cap at 8,000 characters and section bodies at 1,800. |
| Debate | Bull argument → Bear rebuttal → Bull defense → Judge, over the same research snapshot; Board handles the decision | Claim/objection/answer identity is split across artifact shapes, rather than one common discussion record. |
| Research queue | Durable pending/processing leases, bounded retries, answer outbox, evidenced completion into the question ledger | Separate from board comments. Questions are claimed for eligible tickers in a later cycle; not a same-cycle inbox addressed to a busy role. |

`dossier_sync.collect_open_questions` extracts `sub_analyses_requested` from final artifacts and queues new questions. The pipeline claims up to three questions per ticker, renews leases, and requires an answer backed by delivered evidence. A board note does not automatically enter that path.

Bear, Board, and Decision Synthesizer already receive the current cycle's alternative candidates through `cycle_candidates_context`; this comparison should remain available when reviewing a ticker.

The old tournament engine and peer-request producer/consumer were already retired. `tournament_result` is still used by live debate-skip markers and historical readers; deleting it by name would break compatibility. `debate_service` is a read/reporting adapter over saved results, not a second agent scheduler.

## Live evidence and its limits

[Aggregate output](benchmarks/whiteboard-collaboration-2026-09-07.json), produced by [the read-only audit script](../scripts/audits/whiteboard_collaboration.py), covers seven days of `cycle-v3-*` boards. Synthetic and default-cycle records outside that prefix are not included; the prefix alone is not proof every included row is a production investment decision.

- Market context: 98 writes on 93 ticker/cycle boards.
- Quant signals: 84 writes on 84 boards.
- Fundamental reports: 66 writes; risk flags: 34 writes on 32 boards. Mandatory prompt language does not guarantee the separate note is produced. These are aggregate counts, not a paired coverage calculation.
- Bull arguments: 78; Bear rebuttals, defenses, and judge artifacts: 75 each. This verifies the active review path is used, not that it improves returns.
- Annotations: 95, mostly on risk flags (36), desk notes (30), and market context (21).
- No duplicate active section groups in this sample. No annotations linked to superseded entries in this sample; the revision issue below is reproduced offline, not claimed as an observed production incident.
- Research queue at capture: 173 pending deep-dive items and one pending lead; no other status rows. This snapshot does not prove the recently deployed consumer fails, and it supplies no evidence of completed queue work yet.

Publication counts do not establish that a recipient read a particular note, answered it, changed its decision, or earned a better return. Current prompt hashes and contract receipts do not supply per-note proof. More discussion is not itself a performance metric.

## Cleanup implemented in this batch

1. **Scoped annotations.** Previously an agent could submit an entry ID from another cycle and the tool would write to it successfully. The tool now supplies trusted cycle/ticker context to the database lookup before mutation. Same-cycle cross-ticker attempts are also rejected when ticker context is available; internal exact-entry callers retain their existing interface.
2. **Revision provenance.** Reads now retain each annotation's `entry_id` and whether it applies to the active version. Prompt summaries label current, earlier, or unknown versions. Older concerns remain visible rather than silently disappearing or appearing to describe revised evidence.
3. **Honest empty reads.** An absent section no longer asserts its author has not run or that another read can never change the result. It reports unavailable content and unknown producer status.
4. **Reachable collaboration instructions.** The Fundamental prompt now points to the earlier Junior's notes instead of the later Quant's signals. Missing entries do not justify inventing an ID or a disagreement.
5. **Retired peer path removed from the adapter catalog.** `request_peer_analysis` remained in both schema files after its backend producer/consumer were deleted. Both canonical advertisements are removed. The build also regenerates backend and dashboard catalog copies; the dashboard requires its own catalog refresh. `whiteboard_write` now describes versioned notes, reserved artifacts, and the absence of task dispatch accurately.

6. **Catalog generation before consumer builds.** Post-deploy inspection caught the backend packaging an old flat catalog while the adapter regenerated it in parallel. Both backend and dashboard pre-build hooks now generate from canonical split sources before Docker takes their build contexts. Publication uses an atomic file replacement so concurrent consumers cannot read a partially written catalog.

The initial Python reproduction was 1 passing cancellation characterization and 3 failing bug checks. After fixes and additional valid/cross-ticker/legacy cases, all 6 pass. Relevant Python collaboration/debate suite: **105 passed**, plus **33 role/tool-policy checks** (138 total), plus **13 catalog-publication/deployment checks** (151 total). Adapter suite: **662 passed in 31 files**; production TypeScript build passed. No model or full trading-cycle quality benchmark was run for this batch.

## Concrete consolidation design — next implementation, not shipped here

Use the current ticker/cycle workspace as the public interface, with a shared record envelope:

`id, ticker, origin_cycle, author, kind, subject_ref{id,version}, evidence_refs, created_at, operation_id`

Kinds are evidence, note, question, objection, answer, and resolution. Evidence remains versioned; discussion attaches to the exact evidence revision. Agent identity and scope come from trusted execution context. Cross-cycle research carries explicit provenance rather than silently attaching to the new cycle's board.

1. **One publication boundary.** Centralize typed artifact persistence, whiteboard projection, and an event outbox in one service. Retain the existing SharedDesk and phase contracts as views while migrating callers. Commit an operation receipt atomically with the record; retries with the same operation ID return the original outcome. Event delivery is idempotent and recoverable after a crash. Stop awaiting arbitrary notification callbacks as the write's acknowledgement boundary.
2. **One actionable-work path.** Add question/objection records linked to existing `v3_research_queues` items. Reuse its lease token, heartbeat, outbox, and evidence verification. Add target role, priority, decision relevance, and disposition. Present pending → claimed → answer available → reviewed/resolved, with explicit blocked/deferred states. Producing an answer is separate from accepting that it resolves the objection. Ordinary notes never create work automatically.
3. **A bounded inbox at dispatch boundaries.** Before each role starts, deliver unresolved items addressed to that role plus relevant new evidence, with exact IDs/revisions and a delivery receipt. Busy agents finish their current turn. Only the orchestrator may schedule bounded follow-up work; if the target has already finished or the budget is spent, the item is explicitly deferred. A notification must not imply the recipient saw it.
4. **Debate as review of workspace items.** Give Bull claims stable IDs; Bear objections reference those claims; defense answers reference objections and their evidence; Judge records a resolution or unresolved disposition. Preserve fair reply order, the same evidence snapshot, incomplete-answer handling, dissent resolution, and risk gates. Render debate as a filtered workspace timeline rather than another transcript store. Preserve historical tournament/skip readers until their data has a supported migration.
5. **Decision and ranking receipts.** The Board cites which evidence and resolved/unresolved objections changed the entry thesis, risk assessment, and candidate ranking. Carry material unanswered questions into the decision and existing policy checks. A claim's acceptance must depend on evidence rather than how many agents repeat it.

For example, Quant can attach “the debt ratio conflicts with the filing” to the exact Fundamental report revision and request a check. If Fundamental is busy, the item waits for its next permitted dispatch; if its turn is over, the orchestrator explicitly defers it. Defense may answer from existing supplied evidence, but an unresolved request remains visible to the Board with its effect on the decision. The Board records whether the answer resolves the concern. Nobody has to poll an empty section or assume silence means agreement.

Acceptance should require: no lost or duplicated writes after timeout/restart; no cross-cycle delivery; every actionable item owned, resolved, or explicitly deferred; no repeated analyst invocation from duplicate publication; and preservation of debate/risk-gate regressions. Compare the current pipeline and consolidated path on identical frozen inputs with fixed model/settings: completed decisions, grounded objection resolution, unanswered material questions, tool calls, tokens, and latency. Only then evaluate candidate ranking and subsequent outcomes on held-out periods. Profit improvement remains unproven until that evaluation exists.

## Deployment

Completed and independently verified on the NAS at approximately 21:24 PDT, September 7.

| Container | Running revision | Verification |
|---|---|---|
| trading-service | `28c17ae6` | Healthy; `/health` OK; HTTP `/api/v1/agent-tools` returns 90 tools, retired peer absent, corrected whiteboard description. |
| lazy-agent-service | `dae4d65` | Healthy; `/health` OK; actual MCP `tools/list` returns 90 tools, retired peer absent. Signed forbidden no-op call returns `PERMISSION_DENIED` with `isError=true`. |
| trading-client | `0b5e815a` | Healthy; web port 3030 and API port 8888 health OK; HTTP `/api/v1/tools` returns its 55 trading-owned tools, retired peer absent, corrected description. |

Both deploy-kit runs exited successfully. The first targeted trading-service/lazy-tool-service rollout revealed the stale backend catalog after transfer/restart despite healthy containers. The build-order repair was validated and then deployed with trading-service/trading-client. All catalogs above were checked through the running application interfaces after startup, not inferred from build success. Whiteboard implementation source hashes on the NAS matched the validated local files. The pipeline was idle at the deployment gates; no active cycle was interrupted.

The larger inbox/ticket/debate record consolidation remains the design above. This batch does not claim to have shipped that redesign, guaranteed write cancellation, or measured better trading returns.
