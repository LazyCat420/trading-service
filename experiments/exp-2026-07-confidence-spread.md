# exp-2026-07-confidence-spread

**Status:** stopped — REJECTED
**Started:** 2026-07-19 · **Stopped:** 2026-09-04

## Hypothesis (one sentence, falsifiable)

The desk's uniform stated confidence (65–74 on nearly every decision — flagged
by the decision audit as capping the calibration term at 0.85) hides real
conviction differences; instructing the synthesizer to spread confidence
across the full scale will improve calibration (lower ECE, higher
discrimination) without hurting directional accuracy.

## The change

`CHALLENGER_SPEC` custom_instructions, verbatim:

```
CONFIDENCE DISCIPLINE: Your stated confidence must reflect genuine conviction,
using the full 0-100 scale. Reserve 70+ for decisions where the desk evidence
is strongly aligned (multiple independent bullish/bearish signals, no
unresolved dissent). Use 40-55 when evidence is mixed or thin. A confidence
that would be the same for every ticker is a wasted signal — differentiate.
```

## Pre-registered decision rule (do not edit after start)

- Primary metric: paired-disagreement e-value from `/api/v1/challenger/stats?label=exp-2026-07-confidence-spread`
- Promote if: e_value ≥ 20 with leader = challenger AND regressing_sectors = []
- Secondary promote signal (calibration experiments rarely flip actions):
  challenger confidence stdev ≥ 2× champion stdev over ≥ 30 pairs AND
  challenger ECE (computed offline on resolved challenger outcomes) < champion ECE
- Reject if: e_value ≥ 20 with leader = champion, OR challenger directional
  accuracy on disagreements trails champion after ≥ 10 informative pairs
- Give up if: fewer than 10 informative pairs after 21 days
- Goodhart tripwire: grounding/citation judge scores must not fall > 10% vs the trailing week

## Result (2026-09-04)

- **Informative pairs: 25 · e-value: 76,884 · leader: champion (24–1)**
- Pairs logged 522 over 47 days · agreements 469 (89.8%) · disagreements 53,
  of which 38 were graded on both sides and 13 were ties.
- Slices — every sector with a real margin regressed against the challenger:

  | sector | disagreements | informative | champion | challenger |
  |---|---|---|---|---|
  | Information Technology | 14 | 4 | 4 | 0 |
  | Financials | 12 | 8 | 8 | 0 |
  | Communication Services | 7 | 4 | 4 | 0 |
  | Health Care | 3 | 2 | 2 | 0 |

- Secondary signal (challenger confidence stdev ≥ 2× champion's over ≥ 30
  pairs): **not met, and not close.**

  | | champion | challenger | ratio |
  |---|---|---|---|
  | stdev, all 522 pairs | 8.57 | 8.60 | **1.00** |
  | stdev, last 30 days | 4.51 | 4.97 | 1.10 |
  | mean | 64.03 | 61.05 | −2.98 pts |

  The instruction moved the mean down about three points and did not widen the
  spread at all. That is the hypothesis failing on its own terms, independently
  of the outcome test.

### Decision & rationale

**Rejected**, per the pre-registered rule *"Reject if e_value ≥ 20 with
leader = champion"*. Both the primary rule and the secondary signal are
against the challenger, so there is nothing left to collect.

The experiment cost one extra full decision-agent LLM call per ticker per
`v3_full` cycle for 47 days.

### What this experiment got wrong, for the next one

Two design faults made it much weaker than its pair count suggests, and both
are worth fixing before another challenger is started:

1. **It graded the wrong quantity.** `agree` is written as pure action
   equality (`app/v3/challenger.py`), so 469 of 522 pairs were discarded as
   "agreements" — while **269 of those pairs carried a confidence gap wider
   than the panel's own ±3 pt noise band**. A confidence experiment whose
   primary metric is blind to confidence can only ever conclude by accident.
   `confidence_effect()` in `app/routers/challenger_router.py` now measures
   the treated quantity directly; the numbers in the tables above come from it.
2. **Its secondary metric lived only in this file.** The stdev-ratio rule was
   never computed anywhere in code, so for 47 days nothing could have reported
   that the treatment had failed. It is computed now.

A third issue is not specific to this experiment but bounds what any of these
results mean: both sides resolve `entry_price` against the **latest close**
rather than the close at entry + 7 days (`challenger.py`,
`app/autoresearch/outcome_tracker.py`), so the "7-day contract" stamped on the
panel is not the horizon actually measured.
