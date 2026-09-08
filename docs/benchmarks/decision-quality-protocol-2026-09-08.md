# Frozen decision-quality protocol v1

Preregistered before scored inference. Corpus: tests/benchmarks/fixtures/decision_quality_v1.json.
This evaluates reasoning and workflow reliability on synthetic held-out evidence, not
investment returns. It cannot establish that a market action is optimal.

## Cohorts and comparisons

1. Research handoff: four cases × two ticket modes × two repetitions = 16 workflows,
   Fundamental → Quant → Board, 48 role attempts. Counterbalance AB/BA by case and
   reverse in repetition two. Both modes receive identical questions and evidence;
   ticket mode adds the production claim contract, control uses unassigned notes.
2. Other active roles: Junior, Valuation, Bull, Bear, Defense, Judge, Regime,
   Synthesizer and Delta × headroom/missing-history = 18 independent role attempts.
   Use fixed peer artifacts so another model's failure cannot masquerade as this
   role's regression. These are current-contract checks, not a before/after claim.
3. Board method/memory ablation: four cases × three arms = 12 role attempts on identical
   fixed research. Arms: current reviewed method; reviewed method omitted; current
   method plus deliberately stale, conflicting historical memory. The last arm is an
   adversarial exposure probe, not a recommendation to enable unreviewed memory. The
   production boundary is tested separately. Canonical serving and generated proposals
   remain disabled globally. One repetition makes this exploratory.

Total: 78 role attempts, submitted serially. Record every result, including failures;
no selective reruns or prompt repairs after inspecting scores. Infrastructure failures
before inference are setup failures. If execution is interrupted, preserve all completed
rows and hashes; label any resumed cohort and never discard unfavorable observations.

## Fixed execution and boundaries

Use GLM-5.3-Flash-EXL3 through the owned model shim, temperature/min_p zero,
thinking disabled, 8192 output tokens, six tool turns, 600 seconds per role.
Use actual V3 prompt assembly, parser, minimum artifact validation, whiteboard and
research receipt code against an asserted disposable Mongo database. Explicitly invoke
only; default test discovery must not start model calls. Role budgets differ from
production, and the provider harness/automatic repair loop is replaced by the replay
loop, so production transport and full-cycle claims require separate live validation.

All tool schemas come from the current canonical catalog and role grants. Whiteboard
operations use the isolated store; evidence tools return labeled frozen evidence for
EVLT and the two supplied peers only. Missing measurements are explicitly unavailable;
no external market fetches, execution, equations persisted outside the fixture, learning
promotion or production-state mutations. Disabling chart persistence prevents hidden
price requests. Record invalid arguments, missing fixture coverage and role failures
separately. A healthy replay driver is not a healthy agent.

Reviewed baseline text uses the production prefix format. Legacy playbook tips and
live dynamic retrieval are excluded from all arms to keep the evidence frozen. Current
runtime receipt audits independently check what production actually delivers. Freeze
source/corpus/protocol hashes before inference; any code change affecting scored output
requires a separately labeled cohort. Never put scoring keys or expected answers in
model prompts.

## Scoring, fixed before inference

Automated: final role outcome, minimum schema, entry contract where applicable,
answer acceptance and source matching, invalid tool arguments, redundant exact calls,
reported input/output/cached tokens, completeness of usage and elapsed time. Unknown
failed-request usage stays unknown; zero is not imputed. A replay deadline is a failure.

Manual, per artifact, against the supplied dated evidence:

- Factual support: correct versus unsupported/wrong material claims, with excerpts.
- Arithmetic and units: every applicable corpus check, including PEG, range geometry,
  holding return, reward/risk and position headroom. Missing calculation is distinct
  from a wrong calculation; equivalent units/rounding are accepted.
- Uncertainty: missing or conflicting facts remain explicit through the Board; absence
  does not become confirmation. Older filings remain historical rather than current.
- Source independence: repeating one filing does not create independent corroboration.
- Decision consistency: action, entry mode, trigger purpose, proposed price, held state
  and incremental exposure agree. No SELL without holdings; no immediate fill at a
  hypothetical price; no added exposure beyond the stated cap. HOLD has no forced size
  test; preserve legitimate uncertainty and do not reward trading frequency.
- Role fulfillment: complete required work, distinguish a missing artifact from an
  incorrect one, and judge objections/defense fairly rather than counting repeated votes.

A decision is evidence-consistent only if it has a usable artifact, no material false
claim, and no applicable decision/risk-contract violation. Report each dimension and
all denominators instead of hiding tradeoffs in one composite score. Review outputs
under stable shuffled labels where practical; this is one evaluator and not an
independent blinded panel. Retain review rationales and output hashes.

Primary comparison: paired counts of evidence-consistent Board decisions and material
reasoning errors; semantic answers separate from contract-accepted answers. Secondary:
role completion, unresolved questions, costs and latency. Four cases are the independent
units; repetitions do not manufacture eight independent situations. Report per-case
results and uncertainty, never infer general profitability or stable percentage gains
from this small adversarial set. Shared GPU load/cache prevents a controlled speed claim.

## Execution invocation

Use a unique output directory and a disposable database whose name begins with
`trading_bot_pytest_decision_`. Run the explicit benchmark module with
`DECISION_AUDIT_RUN=1 TRADING_BOT_MONGO_TEST=1`; leave DECISION_AUDIT_DRY unset.
The workflow watchdog is 1,900 seconds, covering three separately bounded 600-second
roles; no model calls run in parallel. Freeze source hashes into the output directory
before inference. Dry validation passed 46 workflows before the scored cohort.
