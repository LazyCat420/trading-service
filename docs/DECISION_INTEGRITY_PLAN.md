# Decision-integrity plan — degraded decisions must not look like confident ones

**Date:** 2026-07-25 · **Status:** ✅ **all three phases IMPLEMENTED and deployed** (`eac617a`, `d42785a`)
**Trigger:** "why are all 10 cases HOLDs? doesn't that mean something is wrong
with how we make decisions?"

> **Implementation notes (what changed vs. this plan):**
>
> - **The "all 10 are HOLDs" premise was itself slightly wrong.** The true set
>   is **9 HOLD + 1 BUY** (CPS, 2026-06-24). The all-HOLD reading came from the
>   400-row sample window, not the data. The conclusion is unaffected — it was
>   never a decision bias — but the pattern was weaker still than §1 argued.
> - **Found a second, worse units bug while building the sizing bracket.** HRP
>   weights sum to 1.0 across the **invested** universe, but the injected line
>   called them "% of equity". On a 47%-cash book those differ by ~2×, so VZ's
>   "19.2% of equity" was really **7.9% of equity** of headroom. The board
>   copying that figure was reading a line that was simply wrong, not
>   misreading a correct one. Both the bracket and the original HRP line now
>   state the basis explicitly. See **A4** — the fix is larger than planned.
> - **The provenance filter shipped broken and was caught by its own backfill.**
>   It read `trade_decision` first, so on backfilled desks — whose
>   `trade_decision` predates the field — an explicitly degraded
>   `final_decision` was masked, reporting 0 degraded when there were 7. Fixed
>   in `d42785a`; a filter that fails open on exactly the rows it exists to
>   catch is worse than no filter. Regression test pins it.

---

## 1. The short answer: it is a write-path bug, not a decision bug

**The decision layer is not biased toward HOLD.** Two things made it look that
way, and both are measurement artifacts:

- **HOLD's base rate is 52%**, not ~33%. Of the same 400 `trade_results` rows,
  HOLD 209 / BUY 101 / SELL 90. So "9 missing rows are all HOLD" is
  `0.522^9 ≈ 1-in-344` — genuinely unlikely, worth explaining, but not the
  1-in-a-million a 1/3 base rate would imply. **n=9. Do not over-read it.**
- **Both effects share one cause.** `final_decision` only propagates when the
  board returns `SUCCESS`/`DATA_GAP` (`orchestrator.py:1442`). A board that
  degrades any other way writes no `final_decision` — *and* degrades to the
  conservative default, which the skip path hardcodes as
  `{"action": "HOLD", "confidence": 0}` (`orchestrator.py:798`). Degraded board
  → HOLD, degraded board → missing `final_decision`. The correlation is a
  consequence, not a bias.

Evidence it is the write path and not the decision: **9 of the 10 desks have
`trade_decision` fully persisted and reached `PM_DONE`.** The pipeline produced
a real verdict and saved it to `trade_results`; only the *board's* artifact is
absent from the desk.

### The real defect, and it is worth fixing

**A degraded board is indistinguishable from a confident no-signal HOLD.**
Nothing in the desk marks the difference. `orchestrator.py:1434` shows this exact
failure was already fixed once for board *timeouts* ("deferred item 8.2" — abort
loudly rather than fall through to an unmarked `HOLD@0`); **the fix did not
cover the other degrade paths.** This plan finishes that job.

### Correction to the 2026-07-25 re-test writeup

I reported AMD as proof the no-shorting constraint "works by persuasion —
`coerce_unshortable_sell` never fired." **That was wrong.** It did fire, on the
*synthesizer*: AMD's `trade_decision` carries
`_coerced_from: "SELL"` and `_validator_notes: ["SELL on an unheld ticker is not
executable (no shorting) — coerced to HOLD/no-position; ..."]`. I checked the
container logs, saw no coercion line, and concluded persuasion — **coercion is
recorded in artifact metadata, not the logs.** The Phase 6 *outcome* still holds
(no unshortable SELL escaped, board independently chose HOLD @65), but the
mechanism was the backstop, not the prompt. This is itself a finding: see item
**A3** — a silent guardrail is one nobody can measure.

Notably, **coercion has fired exactly once in 852 desks since 07-01.** AMD was
the first live exercise of that backstop.

---

## 2. What is actually wrong, ranked

| # | Issue | Severity | Evidence |
|---|---|---|---|
| **A1** | Degraded board is indistinguishable from a real no-signal HOLD | **High** — corrupts every accuracy measurement | 9/400 rows; `orchestrator.py:1442` |
| **A2** | `final_decision` silently dropped while `trade_decision` persists | **High** — desk-derived counts are wrong, non-randomly | 10 desks, all HOLD |
| **A3** | Guardrail actions recorded only in artifact metadata, never logged/counted | **Medium** — I misdiagnosed AMD because of this | `_coerced_from` on AMD |
| **A4** | HRP weight read as an order size (`0.192` → `19.2%`) | **Medium** — capped at 10%, but by the cap not the reasoning | VZ, TSM/VZ cycle |
| **A5** | `technicals` 1% fresh; 71-day RSI presented under an "authoritative" header | **Medium** | 5/503 tickers < 3d |
| **A6** | `phase_outcomes` never records the terminal phase | **Low** — cosmetic, but breaks "did this stage run?" | 852/852 desks |

---

## 3. The design principle these all violate

> **A degraded result must never be representable as a confident one.**

Every issue above is the same shape: something went wrong, the system produced a
*plausible-looking* artifact anyway, and the degradation left no trace in the
place a reader would look. This is the same class as the audit's own retraction
(scoring opinions as trades) and the `bot_id` bug (an empty book that looked
like a real one). **The failure mode of this codebase is not crashing — it is
laundering.**

Three rules follow, and they should be the standard for this work:

1. **Every degrade path stamps the artifact.** No silent fallbacks. If the board
   did not genuinely decide, the desk says so in a field the scorecard reads.
2. **Guardrails emit a countable event.** If a constraint fires, it appears in
   logs *and* telemetry — not only in metadata a reader has to know to look for.
3. **Two records of the same decision must reconcile, or the mismatch is an
   alert.** `shared_desk` and `trade_results` disagreeing is a bug, not trivia.

---

## 4. Plan

### Phase A — stop the laundering ✅ SHIPPED (`eac617a`)

*Implemented in `app/v3/shared_desk.py` (`DecisionProvenance`, stamped inside
`append_artifact`), `app/v3/orchestrator.py` (unconditional write + degraded
sentinel), `app/v3/artifact_validators.py` + `app/v3/telemetry.py`
(`v3_guardrail_firings`). 20 tests in `tests/unit/test_decision_provenance.py`.*


- **A1. Add an explicit `decision_provenance` field** to `final_decision` /
  `trade_decision`, always present, one of:
  `board_reasoned` · `board_degraded_fallback` · `no_trade_gate_skip` ·
  `coerced_unshortable` · `timeout_abort`.
  The scorecard then **excludes non-`board_reasoned` rows by default**, the same
  way `--executable-only` excludes unactionable ones. Until this exists, every
  hit-rate in the audit silently includes degraded HOLDs.
- **A2. Make the `final_decision` write unconditional-on-existence.** If the
  board produced *anything*, persist it with its provenance; if it produced
  nothing, persist an explicit `{"action": null, "provenance":
  "board_degraded_fallback"}` sentinel rather than leaving the key `null`.
  `null` currently means both "never ran" and "ran and we lost it" — the exact
  "didn't run vs ran-and-omitted" trap §6 of the audit report warns about.
- **A3. Log and count every guardrail firing.** `coerce_unshortable_sell` should
  emit at INFO and increment a telemetry counter. One line. It is the reason I
  misdiagnosed AMD.
- **A6. Record the terminal phase in `phase_outcomes`.** Cosmetic but it is
  100% of desks, and it currently makes "did the PM stage run?" unanswerable.

**Verification:** re-run the AMD case and confirm the desk distinguishes
"board degraded" from "board chose HOLD"; confirm `verify_audit_phases.py`'s
11th check goes green for the right reason.

### Phase B — reconciliation as a standing check ✅ SHIPPED (`eac617a`, `d42785a`)

*3 new checks in `verify_audit_phases.py`; `--include-degraded` (off by
default) + provenance breakdown in `agent_scorecard.py`;
`scripts/backfill_desk_decisions.py` repaired all 10 historical desks after a
1766-row backup.*


- **B1. Add a `shared_desk` ↔ `trade_results` reconciliation** to
  `verify_audit_phases.py` and the scorecard: any row present in one and absent
  from the other, or disagreeing on action, is reported. This would have caught
  A2 the day it started (2026-07-06) instead of 19 days later.
- **B2. Backfill the 10 known desks** from `trade_results` so historical
  desk-derived counts stop under-counting HOLDs. **Back up first** (standing
  rule), and stamp backfilled rows so they are distinguishable from natively
  written ones.

### Phase C — the two data-quality items ✅ SHIPPED (`eac617a`)

*New `app/quant/sizing_bracket.py` (12 tests, incl. a direct reproduction of
the VZ units failure); HRP line in `context_block.py` restated in INVESTED-
capital terms; `technical_baseline.py` no longer calls a stale baseline
"authoritative". The collector gap itself (C2's first half) is **NOT** fixed —
still only 5 of 503 tickers fresh; that is a data-pipeline job, not an agent
one.*


- **C1. Sizing bracket (the top open item, now unblocked).** Build it as
  planned, but the injected block must **state units explicitly** and label HRP
  a **ceiling, not a size** — VZ's 19.2% came from an agent faithfully copying
  `0.192 × 100`. Ship it shadow-first per the standing board rule.
- **C2. Fix the `technicals` collector** (1% of tickers fresh). Separately, the
  stale branch of `technical_baseline` should **soften the header** — a 71-day
  RSI should not sit under "these are the authoritative values; do NOT estimate
  around them." Cheap, and it is a correctness statement to the model.

---

## 5. Brainstorm — prevention, not just repair

Ideas beyond the immediate fixes, roughly by leverage:

- **Make the sentinel type-level, not convention-level.** A
  `Decision = Reasoned(...) | Degraded(reason)` union (or a required
  `provenance` on the dataclass) makes "forgot to mark the degrade path"
  unrepresentable, rather than something the next fallback path can quietly
  reintroduce. This codebase has now grown *three* separate unmarked-fallback
  bugs (timeout, degrade, `bot_id`); convention is not holding.
- **A "suspicious uniformity" canary.** The tell here was a distribution, not an
  error: 10/10 one value, 80% of sizes in {3,4,5}%, `trend_strength` averaging
  0.81 with zero tool calls. A weekly job that flags *any* agent field whose
  distribution collapses (low distinct-count, or one value >70%) would have
  surfaced the sizing habit, the fabricated RSIs, **and** this bug — all of
  which were found by hand, months apart.
- **Assert on the join, not the table.** Most of this wave's findings came from
  joining two sources that disagreed (`desk` vs `trade_results`, artifact RSI vs
  `technicals`, desk `held` vs `positions`). Standing reconciliations are
  cheaper than audits.
- **Invert the scorecard default.** Make it *opt out* of `--executable-only` and
  `--reasoned-only` rather than opt in. Every wrong headline in this audit came
  from a permissive default; the safe default should require a flag to loosen.
- **Log what was *not* done.** The shed logs are the model to copy — they name
  the dropped sections, which is how we can prove `portfolio_context` survived.
  Coercions, fallbacks and skips should be equally legible.
- **A cheap chaos test.** Force the board to fail (raise/timeout) in a test
  cycle and assert the desk is *marked* degraded rather than merely HOLD. This
  bug class is trivially reproducible on demand and has recurred three times;
  one test would pin it permanently.

---

## 6. What NOT to do

- **Do not "fix" the HOLD rate.** It is 52% because most decisions are screening
  no's, and nothing here shows a directional bias. Tuning the board toward more
  action on the strength of a write-path bug would be the same error as the
  audit's retracted headline.
- **Do not simplify the coercion into an unconditional rewrite.** Same reasoning
  as the quant reconciliation trap in §6 of the audit report.
- **Do not quote the "all 10 are HOLDs" figure as evidence of bias.** With a 52%
  base rate and n=9, it is explained entirely by the shared cause above.
