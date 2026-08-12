# HANDOFF — index

**This file is the entry point, not a session record.** It was itself a session
record until 2026-08-12, dated 2026-08-03, and four sessions of work landed
after it without touching it — so anyone who opened the obvious file got a
picture five days stale. It is now an index, and the rule is:

> A session writes `HANDOFF_<topic>_<date>.md` and adds **one row** here.
> Nothing is deleted for being old — a retired plan is the only record of *why*
> it was retired, and "why not" is the expensive question.

The **served** documentation for this service is
`trading-client/documentation/chapters/`, at
<http://10.0.0.16:8888/documentation>. These files are the in-repo handoffs:
what a session did, in the repo it did it in. Read the chapters for the current
state; read these for the reasoning behind a specific change.

---

## Current

| Date | Handoff | What it settled |
|---|---|---|
| 2026-08-12 | [`HANDOFF_open_item_46_2026-08-12.md`](HANDOFF_open_item_46_2026-08-12.md) | A `HOLD` on a name we OWN is not a decision about entering. Held-aware label, wake pool, dead signal repaired, exit ratchet measured. **Also corrects the "turn the panel on" recommendation** — read its Next step. |
| 2026-08-03 | [`HANDOFF_self_contradictions_2026-08-03.md`](HANDOFF_self_contradictions_2026-08-03.md) | Five places where a comment claimed one thing and the code did another. All five fixed. |

## The July wave — measurement integrity

These are the ones still worth opening. Each was a measurement that changed a
decision, and several are still the only record of *why* something is off.

| Date | Handoff | What it settled |
|---|---|---|
| 07-31 | [`HANDOFF_measurement_integrity_2026-07-31.md`](HANDOFF_measurement_integrity_2026-07-31.md) | Measurement integrity and confidence calibration. |
| 07-31 | [`HANDOFF_open_items_2026-07-31.md`](HANDOFF_open_items_2026-07-31.md) | The above, worked to completion. |
| 07-30 | [`HANDOFF_quant_layer_session_2026-07-30.md`](HANDOFF_quant_layer_session_2026-07-30.md) | The quant-layer review that became a measurement problem. |
| 07-30 | [`HANDOFF_vendor_integrity_2026-07-30.md`](HANDOFF_vendor_integrity_2026-07-30.md) | The one-vendor rule reached 3 call sites. |
| 07-30 | [`HANDOFF_phase0_followthrough_2026-07-30.md`](HANDOFF_phase0_followthrough_2026-07-30.md) | Technicals repaired; the tournament premise re-checked. |
| 07-29 | [`HANDOFF_return_series_integrity_2026-07-29.md`](HANDOFF_return_series_integrity_2026-07-29.md) | The return series the desk reasons from was wrong in three places. |

> [!IMPORTANT]
> **`HANDOFF_tournament_retired_2026-07-29.md` is load-bearing and gets
> re-derived by every session that notices `DEBATE_ENGINE = 3`.**
> The tournament was **measured** and retired: 28.2% of ALL pipeline tokens,
> 374 s/ticker, selection indistinguishable from the free `quant
> thesis_direction`, 6.5× worse on the removal channel, chi2 = 16.63 redundant
> with the quant, Brier **0.3090** vs a base rate of **0.2266** (n=98). It is
> not an accident and not an oversight. Read it before proposing a debate
> engine.
>
> The **probabilistic panel** is a different engine (`DEBATE_ENGINE` 1, with 2
> as the ρ=1.0 control) and has never been the default — so it *is* unmeasured.
> But `scripts/score_panel.py` already names the baseline that decides it:
> **self-consistency**, which is cheaper than the panel and has never been run.
> Run that first.

## The July wave — harness and audit

| Date | Handoff | What it settled |
|---|---|---|
| 07-30 | [`HANDOFF_hooks_followup_2026-07-30.md`](HANDOFF_hooks_followup_2026-07-30.md) | Invariants, hooks, cost accounting. |
| 07-30 | [`HANDOFF_harness_hooks_2026-07-30.md`](HANDOFF_harness_hooks_2026-07-30.md) | Harness audit → invariant hooks. |
| 07-29 | [`HANDOFF_harness_fixall_2026-07-29.md`](HANDOFF_harness_fixall_2026-07-29.md) | The fix wave: sensors repaired, gate hypothesis retired. |
| 07-29 | [`HANDOFF_harness_audit_2026-07-29.md`](HANDOFF_harness_audit_2026-07-29.md) | Systematic cycle audit: what is actually broken. ⚠ Its "tournament discriminates at p=3.2e-09" claim is **refuted** by the tournament-retired handoff — that association measures the board *listening*, not the verdict being right. |
| 07-29 | [`HANDOFF_fidelity_audit_2026-07-29.md`](HANDOFF_fidelity_audit_2026-07-29.md) | Agent fidelity, accounting, tool-selection audit. |
| 07-29 | [`HANDOFF_tool_attribution_2026-07-29.md`](HANDOFF_tool_attribution_2026-07-29.md) | Tool attribution: half-fixed, the other half cross-repo. |
| 07-29 | [`HANDOFF_simplification_and_panel_2026-07-29.md`](HANDOFF_simplification_and_panel_2026-07-29.md) | Simplification wave, confidence collapse, and the panel's design + scorer. |
| 07-29 | [`HANDOFF_tournament_retired_2026-07-29.md`](HANDOFF_tournament_retired_2026-07-29.md) | See the callout above. |
| 07-28 | [`HANDOFF_valuation_agent_2026-07-28.md`](HANDOFF_valuation_agent_2026-07-28.md) | Valuation Analyst, mined doctrine, opinion cards. |

---

## Where else to look

- `ARCHITECTURE.md` — what this service owns, and the *Rules for Future
  Development* that outlive any one session.
- `AGENTS.md` — harness-level pipeline constraints. The source of truth for
  what an agent call may do.
- `docs/` — longer-form plans and one-off audits
  (`JURY_VETO_SCORECARD_2026-07-29.md`, `PLAN_execution_realism_and_statistical_rigor.md`,
  `trading-cycle-verification-checklist.md`).
- `reports/` — generated cycle reports and `verified_fixes_history.md`.
