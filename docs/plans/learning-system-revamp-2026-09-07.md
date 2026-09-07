> Ownership correction from the user: Prism is read-only external infrastructure. Implement all proposed Prism adaptations through lazy-agent-service; do not modify or deploy Prism. The implemented scope and limitations are recorded in ../learning-contract-v2.md. Original release outline below is historical planning context, not authorization to edit Prism.

# Learning-system revamp plan — 7 September 2026

Status: proposed implementation plan. This document authorizes no claims that fixes or migrations have already happened.

Evidence: the September 7 audit at `/home/lazycat/github/projects/sun/.scratch/autoresearch-memory-audit-20260907/AUDIT.md`, including live database snapshots and NAS logs. Recheck active document hashes, source heads and deployed versions before implementation; the pipeline continues producing records.

## Objective

Make agents reach valid, evidence-supported results with less repeated research, fewer tool calls and lower total latency/token cost, while preventing learned guidance from overriding agent roles, risk policy or verified facts.

Completion means the full write → validate → retrieve → deliver → evaluate → retire loop works across trading-service and Prism, its failure modes are observable, legacy writers cannot bypass it, and measured results justify each enabled component. A green unit suite or successful deployment alone is insufficient.

## Target design and ownership

Use one logical learning contract with adapters to existing storage. Do not begin by merging databases or introducing another autonomous agent framework.

| Knowledge class | Authority and owner | Allowed use |
|---|---|---|
| Agent role, decision schema, risk rules | Versioned application code/configuration; trading-service | Binding policy; excluded from autonomous skill rewriting |
| Source evidence and historical artifacts | Original source/producer; immutable references | Audit and verification; never automatically treated as instructions |
| Time-sensitive facts | Trading domain validator, with explicit as-of/validity | Evidence only when applicable to ticker and requested time |
| Reusable procedures and role skills | Candidate producer plus independent validator/evaluator | Bounded suggestions for how to do the task; cannot introduce policy |
| Operational incidents and remedies | Harness/operations owner | Route to relevant maintenance work; do not inject infrastructure fixes into every analyst |
| Research questions and verified answers | Existing research queue and evidence receipts | Reuse actual answers; preserve original question date and unresolved states |

Trading-service owns domain truth, final artifact validation, role scope and the outcome ledger. Prism owns generic storage, retrieval and workflow extraction. Trading supplies an authenticated final-deliverable receipt to Prism; Prism does not independently guess whether trading succeeded. Existing verified-research contracts should be extended, not replaced with another queue.

A shared schema specifies IDs, provenance, scope, lifecycle and receipts. Each service enforces the fields it owns. Cross-service contract fixtures prevent incompatible releases. General chat/coding memory remains isolated from trading-specific policies.

Minimum fields:

- Identity: record ID, schema version, kind, content hash and content revision.
- Scope: project/profile, agent role, ticker/entities, task/failure signature and applicable tool/schema versions.
- Provenance: full cycle/run/artifact IDs, source references and supporting excerpts; producer model/prompt/version.
- Time: observed-at, source-as-of, valid-from/to, last verified-at and next review. Updating prose never updates verification time automatically.
- Lifecycle: candidate, validated, shadow, active, quarantined, superseded or retired; reason and replacement/parent IDs.
- Evidence status: validation receipt, sample size and measured outcomes. Unknown is distinct from zero, failure and success.
- Index status: embedding model/version, content revision, pending/succeeded/failed; lexical retrieval remains possible when vectors are unavailable.

Keep lifecycle/usage events append-only alongside mutable eligibility. Separate `retrieved`, `delivered`, `applied` and `effective`: none implies the next. Agent claims of application are advisory until linked to artifacts or tool receipts.

## Release 1 — Contain bad guidance and expose failed learning

Start here before repairing writers, backfilling embeddings or activating canonical memory. Otherwise the repaired system spreads existing bad rules more efficiently.

1. Export the active skills and relevant memory versions with hashes and provenance. Identify conflicting clauses in all seven live skills. Preserve history; quarantine the affected versions and install a reviewed baseline of role-specific methods without invented risk thresholds, forced directional calls, confidence manipulation or blanket exclusion of old filings.
2. Introduce explicit per-project/per-component controls for skill serving, proposals and memory classes. Freeze autonomous promotion of affected trading procedures until contract validation works. Keep evidence capture and useful current research running.
3. Separate semantic invalidity from statistical regression. Invalid definitions, forbidden policy changes, missing provenance and role violations disqualify a candidate immediately; statistical maturity governs performance comparisons only.
4. Make missing final-deliverable validation mean `unverified`, never `completed`. Preserve unverified workflow evidence but exclude it from “successful workflow” retrieval. Initially scope this to trading integrations.
5. Persist learning-health states and counts: disabled, empty, pending, failed, stale, quarantined and ready. AutoResearch may finish while a learning component is degraded; report both accurately.

Files: trading `skill_loader.py`, `skill_optimizer.py`, `agent_runner.py`, `core.py`; Prism workflow extraction and harness lifecycle; parameter/feature controls.

Exit: bad skill versions cannot reach a trading prompt; no absent validation receipt becomes success; component switches are independent and observable. Test the seven actual skill texts as semantic-contract cases, including permitted older filings and legitimate HOLDs. Domain validation must not depend solely on keyword regexes or an LLM verdict.

Rollback: revert serving to the reviewed baseline, preserve capture and audit records. Do not automatically restore known-invalid skills.

## Release 2 — Close lesson and workflow writes

1. Fix the poison-guard import, preserve complete cycle IDs and replace blind 120-character truncation with structured lessons and a separately rendered brief.
2. Route both reflection recommendations and evaluator lessons through one idempotent writer. Capture source excerpts and distinguish incidents from reusable methods. Use exact duplicate keys first; cluster near-duplicates for review without merging distinct tickers, dates or qualifications.
3. Use a durable outbox for indexing and cross-service delivery. Store validated evidence before indexing; retries cannot duplicate records. Recovery must tolerate a crash between any two steps.
4. Carry trading's artifact validation receipt back to Prism, bound to the exact run and artifact digest. Reject model-supplied success flags. A repaired output gets its own receipt; a failed original trajectory remains distinguishable.
5. Reconcile the audited lesson corpus against embeddings after quarantine/classification. Backfill eligible records only; 404 missing embeddings are not permission to promote 404 lessons.
6. Reduce duplicative evaluator work. Deterministic known failure signatures produce incident updates; an LLM reviews novel/ambiguous evidence with chronology and relevant delivery receipts. Retire random trace-slice grading only after the replacement covers its useful detections.

Exit: persisted failure results, matching source/index revisions, unique full attribution, idempotent replay and successful reconciliation. Reproduce the observed EMPTY RESPONSE workflow and missing import; neither may silently pass. No real trading records in tests.

## Release 3 — Make retrieval selective, pure and accountable

1. Assemble one per-agent knowledge budget across trading memory, Prism memory/workflows, recent lessons, previous desk context and directives. Identify each item's owner and priority; deduplicate repeated evidence across blocks.
2. Create a compact retrieval query from role, ticker, task, question IDs and evidence gaps. Do not embed the entire analysis prompt or move all dynamic evidence into the system prompt merely to satisfy the retrieval embedder.
3. Preserve required Board/judge/defense/research evidence under existing contracts. Reduce redundant advisory context; do not resolve size problems by silently dropping decision-critical records.
4. Retrieve eligible records by task/entity/tool compatibility and validity before bounded candidate selection. Replace the blanket newest-50/newest-500 gate where it excludes older applicable knowledge. Introduce an index only if measured scale requires it.
5. Render complete records with source IDs, dates and uncertainty. Keep learned content subordinate to role/policy. Distinguish untested procedure, historical observation and validated method.
6. Record the final delivered IDs/content revisions/bytes after all prompt assembly and truncation, in both services. Track exclusions and causes.
7. Make retrieval/preview side-effect free. Current `working_memory.get_context()` marks reminders triggered while formatting them; separate retrieval from delivery and from actual trigger/action acknowledgement. A shadow comparison must not consume a reminder.

Exit: memory-off means zero delivery for that class across both services; shadow retrieval changes no operational state; repeated rendering does not consume work. Final prompt receipts reconcile with actual payloads, including failed/truncated paths. Long prompts no longer force an oversized embedding query.

## Release 4 — Durable consolidation and reversible forgetting

1. Queue eligible tickers independently of whether they are selected for another analysis. Add persistent leases, retry-after, timeouts, backoff, bounded worker capacity and terminal attempt records. Start with a small background budget; do not compete unboundedly with live inference.
2. Validate candidate memory updates before promotion. Restrict updates/deprecations to the source scope; require evidence coverage and atomic or recoverable persistence. A model returning only invalid IDs or deprecations cannot consume all source observations.
3. Use type-specific freshness: market snapshots expire quickly; company filings remain valid until superseded or their stated period is inapplicable; procedures require tool/schema compatibility and outcome review. Preserve historic facts for historic questions.
4. Give every retired record a reason, lineage and reversible status change. Garbage-collect indexes for retired records while preserving sufficient raw evidence for audit and rollback.
5. Connect procedure application IDs to validated task outcomes. Unknown/untested procedures remain candidates; successful completion alone does not establish improved trading performance.
6. Consolidate workflow duplicates into evidence-linked methods; archive redundant raw trajectories from retrieval eligibility. Persist janitor run IDs, eligible/changed counts, exceptions and oldest backlog age.
7. Separate “shown,” “trigger condition met,” “action attempted” and “action completed” for prospective memory and directives. Cycle-count expiry alone must not report an issue resolved.

Exit: the current eligible backlog has documented dispositions and meets an agreed freshness target; crash/retry tests preserve source evidence; expiry never rejuvenates old facts through rewriting. Retirement and rollback leave no orphaned eligibility or index records.

## Release 5 — Prove improvement before promotion

Build a replay corpus from saved evidence, including good runs and observed failures: empty Bull Defense, missing research evidence, stale question premises, contradictory skills, long contexts, outages and legitimate HOLDs. Freeze data availability at the original time; exclude later market information and split tuning from held-out evaluation by time/task.

Compare a reviewed baseline with each component independently: role skills, canonical facts, procedures, Prism memories and consolidated workflows. Then test the combined configuration. The actual baseline already contains multiple memory paths; “canonical off” is not “all learning off.”

Primary metrics:

- Valid artifact completion and evidence accuracy; contradictions, unsupported claims and policy violations.
- Repeated unanswered questions, duplicate tools, failed tools and corrective retries.
- End-to-end latency, total input/output tokens, actual cache reuse and total learning overhead.
- Retrieval relevance, final delivery coverage, useful application and avoided duplicate work.

Hard gate: zero policy-contract violations in the regression corpus and no newly accepted unsupported answers. Predetermine acceptable quality noninferiority and material efficiency improvement before running the experiment; report sample size, paired uncertainty and model/harness versions. Do not use a fixed arbitrary improvement percentage as an after-the-fact success criterion. If evidence is insufficient, remain shadow/limited instead of declaring a win.

Keep resolved trading outcomes as a separate, slower evaluation. Compare mature, compatible cohorts on both sides; account for model/harness/tool changes and correlated versions across roles. Do not shorten investment horizons merely to make learning metrics mature sooner. Do not lower the maturity threshold to unstick bad skills—Release 1 fixes their contract validity independently.

Exit: each enabled component has a defensible benefit or explicit quality purpose, measured total cost and documented failure/rollback criteria. Components without benefit remain disabled or are retired.

## Release 6 — Remove obsolete pathways and complete migration

- Publish a producer/consumer/owner registry; enforce that new learning writes use approved interfaces.
- Retire the dead legacy briefing and research-loop entry points after checking scripts, schedules and external callers. Preserve the newer verified research queue/outbox flow.
- Keep CORAL's patch grader and isolated-worktree utilities under clear code-repair naming; update stale autonomous-loop claims. Keep autofix as a separately evaluated repair capability, outside memory optimization and self-approved deployment.
- Remove redundant global lesson/directive injection once scoped consumers cover it. Update architecture docs and tests that currently encode stale behavior.
- Reconcile every legacy collection into migrated, retained-as-evidence, quarantined or retired. Do not delete by collection name or age alone. Remove compatibility readers after measured migration coverage and an explicit retention window.

Exit: one supported learning contract, no undocumented active producer, no silent write/read failure paths, no stale operational claims in current documentation.

## Integration and NAS release procedure

Implement as sequential reviewable batches, with code, migration dry-run output, meaningful tests and evidence in each batch. Use separate changes for trading and Prism with shared contract fixtures; coordinate Prism work with its repository owner. Preserve unrelated concurrent work.

Deploy compatible readers/receipts first, then producers, then migrate data, then enable consumers. Old unknown records remain excluded from active learned instructions until classified. Avoid simultaneous schema cutovers across services.

After each validated deployable batch, deploy affected service IDs through deploy-kit using `npm run deploy -- --only=<ids> --skip-pull`; verify transfer/restart, running container, HTTP/health and actual application behavior on NAS. Standing deployment authorization applies. Deploy model/prompt behavior gradually with independent switches and watch the next completed natural cycles. A new forced expensive cycle is not a prerequisite for every plumbing change.

Before enabling broader learning, validate prompt receipts and workflow decisions in shadow. Stop promotion on newly accepted invalid artifacts, policy conflicts, scope leaks, missing provenance or material quality regression. Revert the affected component, not the entire pipeline.

## First implementation batch

Containment plus instrumentation: snapshot all active skills; establish reviewed safe role guidance and independent serving/promotion controls; require positive trading artifact receipts for workflow success; expose failed/disabled learning states. Then fix the lesson writer through the new validation path. This order prevents repaired ingestion and indexing from amplifying the invalid guidance found in the audit.
