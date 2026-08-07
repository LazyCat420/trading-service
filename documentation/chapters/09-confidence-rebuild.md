# Rebuilding the confidence scale

Written 2026-08-07. **This is a design, not a shipped change.** Nothing here is
implemented; it exists to be approved or rejected before code moves. The
finding that motivates it is written up in the client's *Open items* as item 9
and is not repeated in full here.

## The problem in one table

| | n | ≥80 | ≥70 (clears floor) | 55–69 dead band |
|---|---:|---:|---:|---:|
| Before 08-03 | 712 | **26.0%** | 59.8% | 38.3% |
| After 08-03 | 143 | **0.0%** | 12.6% | **85.3%** |

Not one decision in 143 has reached 80 since 08-03. The scale still carries
signal — `confidence_audit.py --calibrate` puts the empirical win rate at
**0.912** for calls stated at 80+ and ~0.55 at 55–60 — but the desk no longer
reaches the band where that signal lives. Raw confidence now scores Brier
**0.2592** against a base rate of **0.2527**: *worse than knowing nothing*.

The compression originates upstream, in the six research analysts. The
synthesizer's output spread (5.15) is **wider** than the mean of its inputs
(3.85), so it is inheriting the problem, not creating it.

## Why rebuild rather than repair

The specific 08-03 cause was deliberately left unattributed (see item 9 for
what was ruled out). That is a defensible call only under an architecture where
the model does not choose the number, because **whatever compressed the scale
once can compress it again**. That asymmetry drives the whole design:

| approach | survives an unknown recurrence? |
|---|---|
| Recalibrate the existing number | **No** — refitting forever against a drifting scale |
| Compute confidence from evidence | **Yes** — the model cannot choose it |
| Rank instead of score | **Yes** — relative order survives a uniformly drifting scale |

So recalibration is a **bridge**, not a destination. Shipping it and stopping
leaves us where we started within a month, and this document exists partly to
make that failure mode hard to reach by accident.

## Stage 1 — Recalibration (the bridge)

**Goal:** make the floor mean something again within days, without touching an
agent prompt.

`scripts/confidence_audit.py --calibrate` already fits an isotonic map from
stated confidence to empirical win rate, in-sample on the first half and scored
out-of-sample on the second:

```
50 -> 0.516     65 -> 0.605     80 -> 0.912
55 -> 0.556     70 -> 0.650     85 -> 0.912
60 -> 0.556     75 -> 0.650     90 -> 0.912
```

Out of sample it beats **both** the raw scale and the base rate
(0.2405 vs 0.2592 vs 0.2527). The script's own conclusion is *"worth wiring in
— behind a parameter, measured against the live floor."*

**Constraints this stage must respect:**

- Behind a parameter, defaulting **off**, so the change is reversible without a
  deploy and the before/after is attributable.
- The **floor of 70 stays where it is.** It is calibrated (see the floor
  memo) and moving it in the same window would confound the measurement — the
  exact error that made the 08-05 anchor unreadable.
- Refit on a schedule, not per-cycle. A map that moves every cycle is a second
  drifting scale.
- **Do not** ship this together with any stage-2 work. One change per
  measurement window.

**How we will know it worked:** the share of decisions clearing the floor
recovers toward the pre-08-03 59.8%, *and* out-of-sample Brier stays below the
base rate. Recovery of the first without the second means we lowered the bar
rather than sharpened the number.

## Stage 2 — Evidence-bound confidence (the destination)

**Goal:** the model stops choosing a single blended number.

Split the artifact's `confidence` into two fields with different authorship:

- **`evidence`** — computed by the harness, not stated by the model. A function
  of what the agent actually did and what it actually had.
- **`conviction`** — the model's directional read, which is genuinely its job
  and which it is good at.

The composite that feeds the floor is a function of both. A strong opinion on
thin evidence stops being expressible as a high number, which is precisely the
failure mode a free-text confidence permits.

**Every input below already exists — this needs no new plumbing:**

| input | source | already present |
|---|---|---|
| declared data gaps | `data_gaps` | **required** field in `DESK_NOTE_SCHEMA` |
| tool results that returned usable data | `tool_telemetry`, `v3_agent_telemetry.loops_used` | recorded per run |
| structural gate outcomes | `v3_guardrail_firings` | written on the refusing path |
| price/fundamental freshness | `price_history`, fundamentals recency | known-two-population issue, see the data notes |
| cross-desk agreement | `shared_desk` | quant vs fundamental direction is already stored |

**Open design questions — these are the parts to argue about, not the list
above:**

1. How do the components combine — a weighted sum, a minimum (weakest-link), or
   a learned map? A minimum is the most conservative and the hardest to game;
   a learned map risks fitting to 143 post-compression decisions.
2. Does `evidence` get computed per stage, or once per desk at the end? Per
   stage is more diagnostic; once per desk is far less code.
3. What happens to `conviction` when evidence is near zero? Suppressing the
   desk entirely and recording a drop is cleaner than emitting a low score that
   reads as a considered HOLD — the "failed agent must not read as a decision"
   rule applies here.

**Deliberately deferred:** whether `evidence` itself needs recalibrating. It
will, eventually. Stage 1's machinery transfers directly.

## Stage 3 — Rank instead of threshold

**Goal:** stop asking an absolute question the scale answers badly.

The docs already measure this system as better at **ranking** than at
calibration — the tournament ranks at AUC 0.608 while scoring Brier 0.3090,
worse than a coin flip. A threshold at 70 demands calibration. Ranking does
not.

Selection becomes *top-K this cycle by composite*, not *everything above 70*.
K is a risk parameter and belongs with the other governed parameters.

**The cost, stated plainly:** top-K always selects something. On a genuinely
bad day, a threshold correctly returns nothing and a ranking returns the least
bad option. **A floor is still required as a veto** — ranking replaces the
selection question, not the "is any of this worth acting on" question. Any
implementation that drops the floor entirely is wrong.

## What this design refuses to do

- **Assert a minimum standard deviation in a test.** Spread is not the goal;
  discrimination is. Such a test is satisfiable by making the number noisier,
  and at the live sd of 5.15 a `stdev > 5.0` assertion passes today on the
  broken system.
- **Revert the nine anchored prompts.** They are not the cause; item 9 records
  four independent checks.
- **Trim the 08-03 context channels.** They were severed, agents confabulated
  to fill the gap (the synthesizer saw the board verdict on 1 of 134 decisions),
  and `4e78848` restored them deliberately.
- **Move the floor of 70 in the same window as any of the above.**

## Sequencing

Stage 1, then stage 2, then stage 3 — one measurement window each, decided
2026-08-07. The temptation to parallelise is exactly what produced the
unreadable 08-05 window.
