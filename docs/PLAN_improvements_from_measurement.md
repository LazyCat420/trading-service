# Plan — what the measurements actually justify changing

**Date:** 2026-07-26 · **Status:** proposed
**Trigger:** "create a better plan based on the findings to improve the system"

---

## The one finding strong enough to act on

**The system cannot reliably pick winners. It CAN reliably identify its own bad
decisions — and it currently trades them anyway.**

BUY decisions, resolved against real forward prices:

| Confidence | n | mean P&L | vs always-long null |
|---|---|---|---|
| **< 72** | **135** | **−1.77%** | **−4.64%** |
| ≥ 72 | 693 | +3.77% | +0.90% |
| *all BUYs (the null)* | 828 | +2.87% | — |

The low-confidence gap clears every gate this repo has:

```
Newey-West   t = -6.31  (gate 2.5)          PASS
Bootstrap    CI [-6.36, -3.16], p = 0.0     PASS
Chronological halves:  t = -4.34  AND  t = -3.53   -- holds in BOTH
Out-of-sample:         IS -6.60 -> OOS -5.01       -- persists
```

**The live threshold is `ANALYSIS_CONFIDENCE_THRESHOLD = 65`.** The cliff is
between 72 and 75. So 135 BUYs that the system itself flagged as low-conviction
passed the gate and lost money.

Simulated effect of raising the floor to 72: **+0.90% per decision** (+2.87% →
+3.77%), by *removing* trades rather than finding new ones.

### Why this one and not the others

Symmetric honesty about the same data — the *positive* side does NOT hold:

- "High confidence beats the null by +0.89%" → **t = 1.21, CI [-0.41, +2.45],
  p = 0.215. NOT significant.** The gain comes almost entirely from dropping the
  losers, not from the winners being good.
- Every price factor: **FAIL** over 30 years, three of four negative.
- Residual alpha: **+0.24%, t = 0.65** — indistinguishable from zero.
- Pipeline vs always-long: **−0.80% to −0.97%** net of costs, both samples.

One asymmetry survives measurement: **the model knows when it is guessing.**

⚠ **The DSR reports FAIL here, and that is the wrong tool, not a contradiction.**
Deflated Sharpe tests for a *positive* edge inflated by trial selection; this is
a strongly *negative* effect (Sharpe −0.38). The applicable checks are the
chronological split and IS/OOS, both of which pass. Recording this so nobody
later "discovers" the FAIL and reverses the decision without reading why.

---

## Phase 1 — raise the confidence floor (the only change with an evidence base)

**Change:** `ANALYSIS_CONFIDENCE_THRESHOLD` 65 → 72.

One parameter, already enforced in two places (`_apply_policy_gates` and
`pipeline_service`), already tested, already observable via
`HOLD_POLICY_BLOCKED_LOW_CONFIDENCE` in `v3_guardrail_firings`.

**Ship it as a shadow first**, per the standing board rule: log what *would*
have been blocked for one week without blocking it, confirm the live rate matches
the ~16% historical rate, then enforce. A parameter that silently blocks 16% of
BUYs deserves one week of observation before it binds.

**Verification:** `v3_guardrail_firings` should show
`HOLD_POLICY_BLOCKED_LOW_CONFIDENCE` at roughly 135/828 ≈ 16% of BUY attempts.
Materially higher means the live confidence distribution has shifted and the
threshold needs re-fitting, not that the finding was wrong.

**Rollback:** revert the parameter. No code change, no migration.

---

## Phase 2 — make the calibration finding a standing measurement

The finding above came from a hand-written query. It should be a report that
runs, so the threshold can be re-fitted as data accrues rather than rediscovered.

`scripts/calibration_report.py`:
- Hit rate and mean P&L by confidence band, per action, against the always-long
  null over the same rows.
- Newey-West + bootstrap on each band's gap vs the null.
- **Chronological split on every band** — the check that made Phase 1
  trustworthy, and the one most likely to be skipped.
- Explicitly prints the *current* `ANALYSIS_CONFIDENCE_THRESHOLD` next to the
  fitted cliff, so drift between them is visible rather than inferred.

This is cheap and it is what turns a one-off discovery into a maintainable input.

---

## Phase 3 — close the data-integrity gaps found on the way

Small, unambiguous, each independently verifiable:

1. **A malformed action reached `trade_results`:** one row has
   `action = 'BUY|SELL|HOLD'`. The unparseable-action gate added on 2026-07-25
   catches this at the policy layer now, but the writer still accepts it. Reject
   at the write, not only at the gate.
2. **`policy_action` is NULL on 550 of 612 rows.** Expected for rows predating
   the column, but it means gate-effectiveness cannot be measured historically.
   Nothing to backfill — record the limitation in the report rather than let a
   future reader treat NULL as "no gate fired".
3. **`portfolio_snapshots.realized_pnl` / `unrealized_pnl` are NULL in all 25
   rows.** The equity curve — the only true bottom line — is unpopulated. Fix the
   writer. This is the ground truth everything else approximates.
4. **Three bot_ids** (`test_bot`, `test`, `cycle-backend`) split positions and
   fills across what should be one book.

---

## What this plan deliberately does NOT do

Stated because each is tempting and none is supported:

- **Not tuning the agents, prompts, or debate structure.** No measurement here
  attributes outcome differences to any of that, and `decision_outcomes` carries
  no `agent_name`, so per-agent attribution is impossible today.
- **Not adding factors, an RL overlay, or a graph runtime.** Price factors are
  dead over 30 years; adding more is more trials against the same data, which the
  DSR now correctly punishes.
- **Not claiming the system is profitable.** It trails always-long by ~0.9% net.
  Phase 1 narrows that gap; it does not close it, and buy-and-hold remains the
  better strategy on this evidence.
- **Not touching the skill loop.** Attribution shipped yesterday and needs weeks
  of accrual before it can say anything.

---

## The bigger prize, scoped but not built

**Blinded evaluation** ([KTD-Fin](https://arxiv.org/html/2605.28359)): anonymize
tickers and dates so an agent cannot lean on memorized narratives. Their result —
agent rationales shifted from *"defensive blue-chip"* to actual factor ranks, and
**9 of 10 models showed negative selection alpha**.

That is the experiment that would answer whether these agents reason or recall,
which is the question the whole harness exists to serve. It needs alias mapping
across every tool's arguments *and* returns (an agent must never see a real
identifier "even transiently through a tool result"), plus a de-anonymization
probe to certify the mask. Multi-day, harness-wide, and worth it — but only after
Phases 1-3, which are cheap and unblock nothing else.

---

## Expected outcome, written before running it

Phase 1 improves per-decision P&L by **~+0.9%** and closes most of the −0.97%
gap to the always-long null. It will **not** produce a positive edge — it removes
bad trades rather than finding good ones, and the ceiling of that is the null
itself.

If the live block rate comes in far from ~16%, the threshold was fitted to a
distribution that has since moved, and Phase 2's report is what catches it.
