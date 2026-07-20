# exp-2026-07-confidence-spread

**Status:** running
**Started:** 2026-07-19 · **Stopped:** —

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

## Result (fill on stop)

- Informative pairs: — · e-value: — · leader: —
- Slices: —
- Decision & rationale: —
