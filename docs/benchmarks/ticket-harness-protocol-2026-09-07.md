# Ticket harness ablation protocol

Frozen before benchmark inference. This is a targeted research-to-decision replay, separate from the deployed full-cycle observation `cycle-observe-1788844926` (LULU/MSFT, collection and full analysis, trade=false).

Question: does the existing research-question ticket contract improve evidenced completion and downstream delivery relative to unassigned whiteboard notes carrying the same questions?

- Two synthetic evidence cases: complete evidence and a historical question that current-only evidence cannot answer.
- Fundamental -> Quant -> Board use the actual V3 prompt assembler, parser, artifact delivery and validation. Junior/regime/debate inputs are fixed fixtures, not newly generated stages. This is not a full-cycle latency estimate.
- Two arms: tickets in `research_questions`, versus no tickets with the same question text and IDs in a whiteboard note. Both research arms receive the same answer-format instruction, so success does not depend on the grader accepting only the ticket arm's format.
- Same evidence, role prompts, model, temperature=0, min_p=0, thinking disabled, max output 4096, maximum 6 tool turns per model invocation, 600-second role deadline, and tool fixtures in both arms. These bounded replay limits differ from production and will be reported.
- Two repetitions, AB/BA order reversed by case/repetition: four paired workflows, eight workflows, 24 role attempts before any separately reported repair invocations. Never overlap model benchmark calls with the observed production cycle.
- All database writes use a uniquely named disposable test database. All tool execution is fixture-backed; no orders, browsing, or production memories. Automatic Quant chart persistence is disabled because it can fetch live prices outside the tool loop. Only model requests reach the user's Gold Spark shim.
- Primary: supported research-answer coverage, appropriate unresolved disposition, valid non-degraded role artifacts, and downstream answer delivery. Verified quotes alone do not prove the answer follows from its evidence; review answer substance against the fixture too.
- Secondary: duplicated research, tool errors, prompt/output tokens, latency, automatic repair, and ticket resolution state. No-ticket queue completion is not a meaningful comparison metric.
- Grade both arms against the same question inventory after generation; the control does not gain ticket context during generation. Missing usage is unknown, not zero. Include all fixed-cohort failures; no selective reruns or best-of samples.
- Report this small cohort as diagnostic evidence, not proof of profit improvement or a benchmark of the proposed unified inbox (not yet implemented).

Pre-inference validation: the first live trial (`cycle-observe-1788843390`) was stopped after a cycle-ID extraction defect caused a whiteboard permission failure. Agent-service fix `5736438` preserves exact observation IDs and passed 16 targeted transport tests, typechecking, and the 670-test deployment gate. It was deployed before the replacement observation and any benchmark inference. The stubbed pilot passes both workflows, checks question delivery in every role, and exercises completion/defer receipts. Board persona is fixed to CONTRADICTORY in both arms.

Startup correction before any model inference: initial invocations were blocked by pytest's outbound-HTTP guard at model discovery. The explicit `live_http` fixture is now requested, as required for this authorized live integration benchmark. No model request reached inference in those failed startups.

Outer-deadline correction: the first inferred cohort was interrupted in workflow 7 by the inherited pytest 300-second whole-test timeout, despite the intended 600-second per-role limits. Its six completed workflows and seventh partial workflow are retained as an interrupted pilot cohort. Before the replacement cohort, the whole-test timeout is explicitly 1,900 seconds (three role deadlines plus overhead). All eight workflows are restarted in the same fixed order, with unchanged evidence, prompts, model parameters, and tool limits; no best-of or selective replacement is used. Report the original cohort separately and use the complete replacement cohort for the primary paired comparison.
