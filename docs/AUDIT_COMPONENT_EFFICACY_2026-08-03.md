# Audit — does anything check whether the HMM (or any expensive component) is helping? (2026-08-03)

The question asked: *can we monitor whether the HMM helps the cycle? Is that
what autoresearch does? Does anything turn it off when it fails? Can the
system adjust itself and tell me what needs attention?*

## What the audit found (verified in code, not from memory)

**1. Autoresearch does NOT monitor the HMM — or any quant component.**
`run_autoresearch` (13 phases, per cycle) grades *decisions*
(`outcome_tracker`), *agents* (`evaluator.py`'s peer audit, SkillOpt's
per-version scorecard), and *data* (`data_audit`). Quant components have no
scheduled grading at all. The HMM had been graded exactly once, by hand
(`scripts/grade_hmm_regime.py`, written 2026-08-03); nothing scheduled it and
nothing consumed its result.

**2. There was NO off-switch for the HMM.** Not an env var, not a runtime
parameter, nothing — verified by grepping `hmm`/`regime` across
`app/config/`, `.env*`, compose/entrypoint. The only thing that could stop it
was the 45s quant-block budget, accidentally. Compare: the tournament debate
got `TOURNAMENT_DEBATE_MODE` and `DEBATE_ENGINE` precisely so it could be
measured and retired; the HMM — the next-most-expensive prompt component at
~22–32s/cycle — had no equivalent.

**3. Self-adjustment machinery exists, in three tiers:**
- **SkillOpt** (live, per cycle): bounded self-edits of 7 agents' prompt docs,
  with a maturity-gated scorecard and automatic rollback of measured
  regressions. This *is* a component-efficacy monitor — but only for prompt
  docs.
- **Parameter governor** (live): agents propose bounded config changes;
  loosening auto-reverts on TTL. The only writer of `runtime_parameters`.
- **CORAL auto-code-repair** (deleted 2026-07-31): its two highest-scoring
  patches both reverted a deliberate human decision to satisfy a stale test.
  Deleted on its own measured record; `scripts/grade_patch.py` (human-invoked
  grading) is what survives. **Nothing in this wave re-adds autonomous code
  editing, on purpose.**

**4. "Tell the user" was the weakest link.** One agent-note fires on
`system_health == "critical"`; `cycle_directives` reach *agent prompts* but
have no user-facing route; `v3_invariant_violations` is written every cycle
and read by nothing user-facing.

## What this wave ships

A **component efficacy monitor** (`app/autoresearch/component_health.py`)
that composes the existing machinery instead of duplicating it:

- **Daily grading** (scheduler `component_health_evaluation`, 5:45 PM PT
  weekdays, after the 5:30 posterior snapshot): grades the HMM's stored daily
  posteriors on its own claims — band coverage (Kupiec), the free-baseline
  race (Diebold-Mariano vs a trailing 20-day σ on QLIKE + MSE), and
  operational health (snapshot gaps, stale-tape runs). No P&L: the desk's
  honest MDE is 8.84pp (`scripts/power_report.py`), so "did the prompt line
  move P&L" is unanswerable for ~a year; everything graded here is
  self-validating at daily n.
- **Verdicts**: `insufficient_data` / `healthy` / `redundant` / `failing`.
  The default is KEEP-shaped (`gate_ablation.py`'s bar): `failing` requires a
  demonstrated harm — band understating risk, significantly worse than free
  on BOTH losses, snapshot gap ≥4 trading days, or ≥3 consecutive stale-tape
  fits — never a merely missing benefit.
- **Auto-disable**: 3 consecutive daily `failing` verdicts → the monitor
  proposes `HMM_REGIME_MODE` 0→1 through the parameter governor (audited,
  bounded, reversible), writes a ⚠️ agent note telling you what happened and
  how to re-enable, and records the action in the report row. **It never
  re-enables and never sets mode 2** — a component with no data series could
  never earn its way back.
- **`HMM_REGIME_MODE`** (new runtime parameter): `0` active (today),
  `1` shadow — desk path skips the fit entirely (returning its ~22–32s to the
  budget that starves GARCH/HRP/sizing) while the daily snapshot keeps the
  graded series alive; `2` off (human-only). Fail-open lands on the registry
  default (0): the line is advisory prose, so a transient store failure
  briefly resurrecting it is acceptable, silently retiring a component you
  believe is on is not.
- **User surface**: `GET /api/v1/component-health` (latest verdict + the real
  thresholds, served so the client renders the actual gate) and
  `/api/v1/component-health/history`. Reports persist in
  `component_health_reports`.
- **Shared math**: the grading definitions moved to
  `app/quant/regime_grading.py`; `scripts/grade_hmm_regime.py` and
  `scripts/vol_forecast_race.py` re-import them, so the CLI, the offline
  race, and the in-process monitor can never drift apart.

## First live reading (read-only dry run, 2026-08-03)

```
VERDICT: redundant          (no auto-disable — surfaced for a human call)
window   2026-02-10 .. 2026-08-03, n=119
coverage 4 breaches / 119 = 3.36% vs 5% expected — Kupiec p=0.384, CALIBRATED
vs free  QLIKE t=-0.67 (not different), MSE t=+2.38 (significantly worse)
ops      gap 0 days, stale run 0 — snapshot job healthy
```

Identical in shape to the pre-registered 08-03 experiments (calibrated-but-
too-wide band; vol number loses to free on MSE only). The monitor confirms
the known state and would not fire today.

## What was deliberately NOT automated

- **`redundant` does not auto-disable.** The state LABEL, expected duration
  and switching odds are outputs nothing else produces; whether they are
  worth ~22–32s/cycle was explicitly left OPEN for a human on 2026-08-03.
  The monitor surfaces the verdict daily; flipping `HMM_REGIME_MODE=1` (via
  chat: "propose HMM_REGIME_MODE 1") is your one-line way to take the other
  side of that call — grading continues either way.
- **No auto-code-repair.** The CORAL lesson stands: a loop scored by "the
  tests pass" optimises the tests. This monitor can only flip one bounded,
  code-owned, reversible parameter.
- **No P&L verdicts.** Below the measurement floor; pretending otherwise is
  how the tournament survived four audits.

## Files

| Change | File |
|---|---|
| Monitor + verdicts + auto-disable | `app/autoresearch/component_health.py` (new) |
| Shared grading math | `app/quant/regime_grading.py` (new) |
| API | `app/routers/component_health_router.py` (new), registered in `cycle_main.py` |
| Runtime parameter | `app/services/parameter_store.py` (`HMM_REGIME_MODE`) |
| Monitor authorized to propose | `app/validation/parameter_validator.py` |
| Desk-path gate | `app/quant/context_block.py` |
| Mode reader | `app/quant/regime_hmm.py` (`hmm_regime_mode`) |
| Scheduler job + snapshot mode-2 skip | `app/services/cycle_scheduler.py` |
| Script now imports shared math | `scripts/grade_hmm_regime.py` |
| Tests (17 new) | `tests/unit/test_component_health.py` |

Full unit suite at ship time: **2866 passed, 0 failed**.
