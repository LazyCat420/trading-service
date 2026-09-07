# Learning contract v2 — owned implementation

Prism is an external dependency. Never edit, commit to or deploy its repository or modify its memory database. All integration adaptations live in lazy-agent-service. Trading owns domain evidence, skills and outcomes.

| Component | Behavior |
|---|---|
| Reviewed role methods | Exact role/content hashes reviewed in code; seven bounded baseline methods; store failures use the same static baseline |
| Skill proposals | Disabled by default; candidate writes and rollback proposals cannot change serving versions |
| Incident capture | Chronological concrete failure records with full cycle/source attribution; durable indexing retries; lexical recall labelled unverified; no global analyst injection |
| Canonical consolidation | Independent durable queue, 40-source batches, bounded retries and leases; only exact dated quotations are promoted; transactional source archive and partial consumption |
| Retrieval | Canonical eligibility requires v2 provenance and valid dates. Full records fit within 1,800 characters. Generic retrieval cannot bypass learned-content eligibility. Rendering reminders does not acknowledge them. |
| Owned Prism proxy | Preserves the full request evidence and appends a compact role/ticker/question query for upstream retrieval; identifies governed model requests |
| Owned model shim | Removes upstream agent-memory, past-workflows and project-skills blocks before generation; preserves original role, policy and source evidence; logs final payload hashes and exclusions in Trading's database |
| Artifact receipts | Exact original-output identity, typed validation and quality checks; repaired/degraded/weak outputs are ineligible; outbox delivers into Trading's own validation ledger |
| Retirement | Soft eligibility retirement, retained source evidence and lineage; derived retired vectors can be deleted and regenerated |

Independent controls: LEARNING_SKILLS_ENABLED, LEARNING_SKILL_PROPOSALS_ENABLED (false by default), LEARNING_INDEXING_ENABLED, LEARNING_CONSOLIDATION_ENABLED, runtime MEMORY_CONTEXT_ENABLED, and gateway TRADING_LEARNING_BOUNDARY_ENABLED. Canonical serving remains off until a separate relevance/quality evaluation supports activation. Upstream facts/workflows remain excluded; their successful generation is not evidence of usefulness.

The boundary currently requires a configured vLLM shim provider for Trading /agent requests. An unsupported provider is rejected explicitly rather than silently receiving unverified upstream memory. Tool-less /chat requests do not invoke that upstream memory-assembly path. Emergency boundary disable restores the prior external behavior and should be treated as a rollback, not an eligibility approval.

Prism may still run its own background memory extraction and consolidation. We do not claim those external calls were removed. Their outputs are excluded from model delivery by our boundary. This cost remains observable in Prism's existing telemetry and is reported separately from the owned learning improvements.

Native Trading collections: learning_records, learning_events, learning_health, learning_delivery_receipts, learning_gateway_receipts, learning_artifact_receipts, learning_validation_receipts, memory_consolidation_jobs, canonical_memory_evidence, memory_retirement_events, agent_skill_candidates, learning_migrations and learning_legacy_dispositions. Unique IDs and hot-query indexes are maintained by mongo_store. These are native Mongo collections, not additions to the historical Postgres migration map.

GET /learning/health uses Trading's existing API-key authentication and returns persisted component states and counters. Delivery, application and effectiveness are distinct. The gateway records a provider payload; artifact validation links final output to its own identity; neither alone proves improved trading performance.

The legacy LLM briefing rejects calls and names MemoryRetriever.build_memory_brief as its replacement. The old research resolver is retained as a manual compatibility interface; research_work owns the automated verified research queue. CORAL's grader and worktree utilities remain manual code-repair tools, separate from learning promotion and deployment.

Run scripts/learning_migrate.py --output PATH for a dry-run snapshot; add --apply after the gateway is verified. Snapshots are never overwritten. A transaction checks active-skill fingerprints, quarantines unreviewed versions and installs reviewed baselines. Per-record legacy dispositions retain raw evidence. Prism data is never migrated. Disable the affected reader to roll back eligibility; preserve receipts and evidence for review.
