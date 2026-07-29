# The jury veto has never blocked a decision

**Measured 2026-07-29 against the live DB.** Every number carries its window and population.

## Why this was measured first

The tournament debate costs **203s and 239k tokens per ticker — ~31% of all pipeline spend**.
It was shadowed rather than deleted for exactly one stated reason
(`app/services/parameter_store.py:191-194`):

> "The veto is the reason this is a shadow switch and not a deletion: it fired 12 times in 14
> days (`HOLD_POLICY_BLOCKED_JURY_VETO`)."

That claim is load-bearing for a decision about a third of the pipeline's cost, and it had
never been scored. So it was the gate on Stage 3 of the simplification wave.

## The claim does not survive

```
HOLD_POLICY_BLOCKED_JURY_VETO in trade_results.policy_action ...... 0
HOLD_POLICY_BLOCKED_JURY_VETO in v3_guardrail_firings ............. 1
desks carrying tournament_result.vetoed = true ................... 19
```

Not 12 firings in 14 days. **Zero decisions have ever been blocked by the jury veto**, across
the whole history of the table.

## What the 19 vetoed desks actually did

| window | final action | n | what happened |
|---|---|---:|---|
| before 07-23 | BUY | 5 | **executed** |
| before 07-23 | SELL | 2 | **executed** |
| before 07-23 | HOLD | 7 | already HOLD |
| 07-23 onward | HOLD | 4 | already HOLD |

Two distinct reasons the veto never bit, and neither is a bug in the veto itself:

1. **Every veto since the gates became observable landed on a decision that was already HOLD.**
   `_apply_policy_gates` returns `HOLD_NO_SIGNAL` at gate #3 (`orchestrator.py:2108`); the jury
   veto is gate #10 (`:2254`). A HOLD never reaches it. Of the 4 vetoes since 07-23, 3 recorded
   `HOLD_NO_SIGNAL` and 1 has no `policy_action` at all.

2. **The 7 pre-07-23 executions bypassed the gate entirely** — all carry `policy_action = NULL`,
   i.e. `_apply_policy_gates` did not run for them. They were vetoed *and* traded. Checked
   before concluding: this was **not** the Board exercising its override. `overrides_veto` is
   `None` or `false` on all but one desk, and `override_justification` is empty. The gate logic
   at `:2244-2253` is correct; it simply was not on the path.

## Would the veto have saved money?

The 7 vetoed-but-executed trades are the only evidence available of what the veto would have
prevented:

```
XOM  BUY  +2.66%  WIN      C     BUY  -2.93%  LOSS
FCF  SELL -3.34%  LOSS     JPM   BUY  +0.68%  FLAT
AMD  SELL +0.68%  FLAT     AAPL  BUY   0.00%  FLAT
NVDA BUY  +1.99%  WIN

n = 7   mean = -0.04%
```

Blocking these would have changed realized P&L by **+0.04%** — three wins, two losses, two
flat. At n=7 this is not evidence of anything, in either direction. It is certainly not
evidence that justifies 31% of pipeline spend.

## Verdict

The veto is not a safety mechanism that has been quietly protecting the book. It is a flag
that fires, is recorded, and has never once altered an outcome. **The single stated reason for
keeping the tournament does not hold**, and the tournament's own signal was already measured at
`t = -0.17` (n=124).

Deleting the tournament therefore costs nothing that has been shown to have value, and returns
~203s and ~239k tokens per ticker.

**Two honest caveats.** First, n=7 outcomes and 19 vetoes is a small population — this rules
out "the veto is demonstrably valuable," not "the veto could never be valuable." Second, it is
possible the veto would start biting now that gates are properly instrumented and Board
confidence has fallen; but a mechanism that has never fired in anger over the whole recorded
history cannot be the justification for the most expensive stage in the pipeline. If a jury
veto is wanted later, it can be rebuilt as a standalone gate for far less than 203s/ticker.

## Method

The measurement that mattered was not the firing count but the **join to what the decision
actually became**. A firing count alone reads as "the guardrail is working"; joining it to
`trade_results.action` and `policy_action` shows the firing never reached a gate. This is the
same shape as the 07-25 finding that a guardrail "had never fired" when in fact it had — count
the effect, not the event.
