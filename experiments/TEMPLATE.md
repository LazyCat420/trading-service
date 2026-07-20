# exp-YYYY-MM-<slug>

**Status:** draft | running | stopped-promoted | stopped-rejected | stopped-inconclusive
**Started:** YYYY-MM-DD · **Stopped:** —

## Hypothesis (one sentence, falsifiable)

<What the change is supposed to improve, and why you believe it.>

## The change

`CHALLENGER_SPEC` custom_instructions, verbatim:

```
<paste>
```

## Pre-registered decision rule (do not edit after start)

- Primary metric: paired-disagreement e-value from `/api/v1/challenger/stats`
- Promote if: e_value ≥ 20 with leader = challenger AND regressing_sectors = []
- Reject if: e_value ≥ 20 with leader = champion, OR e_value < 1 after ≥ 10 informative pairs
- Give up if: fewer than <N> informative pairs after <D> days
- Goodhart tripwire: grounding/citation judge scores must not fall > 10% vs the trailing week

## Result (fill on stop)

- Informative pairs: — · e-value: — · leader: —
- Slices: —
- Decision & rationale: —
