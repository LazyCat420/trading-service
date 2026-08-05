# Stage-by-stage audit: why every ticker reads HOLD (2026-08-05)

Every ticker on the dashboard has read HOLD for a week. This is the ordered
walk through the cycle, one stage at a time, with a measured verdict on each.

**The finding, up front:** no stage averages the decision away in code. The
collapse is that **ten stages each independently emit a confidence of ~58, and
eight of them were never told what the number means.** The band 55-69 sits
entirely below the execution floor of 70, so the action is HOLD before any
reasoning about the ticker takes place. Adding more analysts added more voices
saying 58.

---

## The measurement that frames everything

Confidence carried by every artifact on the desk, 200 desks since 2026-08-01:

| stage | artifact | n | mean | stdev | distinct values |
|---|---|---|---|---|---|
| 4 | `regime_classification` | 75 | 60.9 | 2.9 | **6** |
| 5 | `desk_note` | 74 | 58.3 | 5.1 | 10 |
| 7 | `quant_report` | 67 | 55.1 | 4.9 | 10 |
| 6 | `fundamental_report` | 73 | 55.7 | 5.3 | 9 |
| 8 | `valuation_report` | 43 | 61.1 | 5.1 | 9 |
| 9 | `bull_argument` | 64 | 58.9 | 6.0 | 12 |
| 10 | `bear_rebuttal` | 63 | 61.1 | 4.0 | 11 |
| 11 | `debate_judge` | 62 | 57.5 | 3.8 | 10 |
| 12 | `final_decision` | 68 | 59.8 | 5.2 | 15 |
| 13 | `trade_decision` | 64 | 58.2 | 5.4 | 13 |

Ten independent stages, every one of them 55-61, none with a standard
deviation above 6. The regime engine — which classifies the *market*, not the
trade — produces 60.9 across **six distinct values in 75 desks**.

A distribution this tight is not a set of ten judgements that happen to agree.
It is one uninformative prior, emitted ten times.

### It is not responding to anything

Board confidence against its own inputs, n=59 desks:

| split | confidence |
|---|---|
| small prompts (avg 59k tokens) | 57.9 |
| large prompts (avg 142k tokens) | 58.0 |
| 1 loop | 55.9 |
| 9 loops | 58.3 |

Prompt size, tool-call depth and data volume move it by less than a point.
Three hypotheses died here — context starvation, prompt bloat drowning the
instructions, and agents failing to do the work. None survives contact with
the numbers.

---

## The stages, in order

### 0. Triage gate — ticker selection
`pipeline_service`, PHASE 0. **Degraded but not the cause.** The gatekeeper LLM
(`deepseek-v4-flash-0731`) returns intermittent empty responses; since `b3d3d90`
this degrades to the scoring engine's top 15 rather than ending the cycle at
zero tickers. Selection works. The tickers it picks are reasonable.

### 1. Context init — `_build_cycle_metadata`
**Verified healthy, and this refutes the obvious theory.** Injected-block
coverage per week:

| week | quant math | technical baseline | valuation | fundamental |
|---|---|---|---|---|
| 07-13 | 0% | 0% | 0% | 0% |
| 07-20 | 52% | 24% | 0% | 0% |
| 07-27 | 100% | 100% | 70% | 64% |
| 08-03 | **94%** | **94%** | **94%** | **94%** |

Coverage is at its all-time high in the week confidence hit zero-above-floor.
The desks are better fed than they have ever been. Note the inverse relation:
in the week the agents had **no** precomputed context at all (07-13), mean
confidence was 74.0 and 81% of decisions cleared the floor.

### 2-3. Precompute + score
`app/quant/*` blocks and, since `aac14ec`, the deterministic baseline. Pure
arithmetic, no model. Not implicated.

### 4. Regime engine → `regime_classification`
**IMPLICATED.** `REGIME_CLASSIFICATION_SCHEMA` defines
`confidence: {type, minimum, maximum}` — **no description**. 6 distinct values
across 75 desks. Separately known to be a coin flip on identical input.

### 5-8. Research: junior → fundamental → quant → valuation
Sequential; each reads the prior artifacts. **Quant is IMPLICATED**:
`QUANT_REPORT_SCHEMA` gives `confidence` no description. `desk_note` has one,
but it reads *"Overall confidence in the findings (0-100)"* — a label, not a
scale. It tells the model nothing about what 58 means versus 78.

Analyst quality also fell over the window while the deciders' rose:

| agent | 07-13 | 08-03 |
|---|---|---|
| `v3_quant_analyst` | 80.2 | **65.4** |
| `v3_junior_analyst` | 79.9 | **69.8** |
| `v3_fundamental_analyst` | 79.8 | **71.4** |
| `v3_valuation_analyst` | — | **60.1** |
| `v3_board_of_directors` | 72.4 | 79.1 |
| `v3_decision_synthesizer` | 77.8 | 84.0 |

The inputs got worse while the output got more polished — a well-formed,
confident-looking HOLD assembled from mush.

### 9-11. Debate: bull → bear → judge
**IMPLICATED, and the timing is the sharpest evidence in this document.** All
three schemas — `BULL_ARGUMENT`, `BEAR_REBUTTAL`, `DEBATE_JUDGE` — define
`confidence` / `final_confidence` with **no description**.

When each agent joined the desk, against the confidence collapse:

| week | tournament | bull/bear/judge | valuation | mean conf | clearing 70 |
|---|---|---|---|---|---|
| 07-13 | 111 | 0 | 0 | 74.0 | **81.0%** |
| 07-20 | 155 | 0 | 0 | 63.1 | 25.6% |
| 07-27 | 83 | 24 each | 133 | 63.3 | 24.6% |
| 08-03 | **0** | **63-67 each** | 56 | 57.3 | **0.0%** |

Four new voices joined the desk (bull, bear, judge, valuation) as the
tournament was retired. Three of the four carry no confidence definition. The
board went from reading ~4 upstream confidences to reading ~8.

**Be careful with this.** The first step down (74.0 → 63.1) happens in the week
of 07-20, *before* bull/bear/judge appear. There are two drops and they have
different timing, so "the debate caused it" is not supportable on its own. What
is supportable: every agent added to this pipeline was added without a
confidence scale, and each one pulls the board's input set further toward 58.

### 12. Board of Directors → `final_decision`
**The only stage with a real scale**, `board_of_directors.py:82-98` — bands at
80-90 / 70-79 / 55-69 / below 55.

**A prose fix here has already failed.** `dcc00af` (07-28) added exactly this
rubric to address exactly this problem. It made things worse:

| window | mean | clearing 70 |
|---|---|---|
| pre-collapse 07-14→20 | 74.0 | 81.0% |
| collapse, pre-anchor 07-26→29 | 63.6 | 19.5% |
| **post-anchor 07-29→08-05** | **59.8** | **13.8%** |

The reason it could not work is structural: the board is the twelfth stage. It
reads nine upstream artifacts that all say 58, and a rubric in its own prompt
does not un-anchor it from its inputs. **Fixing the last stage cannot fix a
pipeline that arrives pre-averaged.**

### 13. Decision synthesizer → `trade_decision`
Has a scale. Emits 58.2. Holds the final say — execution reads
`trade_decision or final_decision`.

### 14. Policy gate — `_policy_action`
**Working correctly, and NOT the cause.** Of 97 recent rows, 83 carry
`policy_action='HOLD_NO_SIGNAL'` with `decision_provenance='board_reasoned'`:
the desk *chose* HOLD. The confidence floor blocked only 9 BUYs. The floor is
calibrated on 1,672 resolved outcomes — expectancy flips sign exactly at it
(65-69 = −1.45%, 70-74 = +4.33%). **Do not loosen it.**

### 15. Execution
Never reached. Nothing clears the floor.

---

## What is actually wrong

**Eight of the fourteen decision-carrying schemas ask a model for a 0-100
confidence without defining the scale.**

| schema | confidence description |
|---|---|
| `QUANT_REPORT` | **none** |
| `BULL_ARGUMENT` | **none** |
| `BEAR_REBUTTAL` | **none** |
| `DEBATE_JUDGE` | **none** |
| `REGIME_CLASSIFICATION` | **none** |
| `DELTA_REPORT` | **none** |
| `DESK_NOTE` | a label, not a scale |
| `FUNDAMENTAL_REPORT` | real anchor |
| `VALUATION_REPORT` | real anchor |
| `FINAL_DECISION` | real anchor (added 07-29) |
| `TRADE_DECISION` | real anchor |

A model asked for an undefined 0-100 confidence returns the middle of the
range. Every time, at every stage, regardless of what it found. The 55-69 band
is where an unanchored number lands — and it sits wholly below the floor of 70.

That is the "statistical slop": not an arithmetic mean anywhere in the code,
but **semantic regression to an uninformative prior, repeated at every stage
and then compounded by anchoring at the board.**

## What follows from it

1. **The scale belongs in the schema, not in one agent's prompt.** Every
   artifact that carries a confidence needs the same anchored bands the board
   has. This is the one change that addresses the mechanism rather than a
   symptom, and it is the opposite of what was tried on 07-28 — which fixed
   the last stage only.

2. **Adding an agent without a confidence scale makes this worse**, by adding
   one more 58 to the board's input set. Any new desk member needs the anchor
   before it ships.

3. **A prose rubric alone is not sufficient** — `dcc00af` is the counterexample.
   The deterministic baseline (`aac14ec`, shadow-only) exists so the board has
   at least one confidence on its desk that is arithmetic and recomputable.

4. **Do not "fix" this by lowering the floor.** The floor is the only
   calibrated component in the chain. The problem is upstream of it.

## Verification note

Everything above is measured against the live database on 2026-08-05, not
inferred from the code. Three of my own hypotheses were killed by these
measurements and are recorded here so they are not re-run: severed context
channels (coverage is at its maximum), prompt bloat drowning the instructions
(57.9 vs 58.0 across a 2.4x prompt-size split), and agents failing to do the
work (SUCCESS rate 90%, quality scores 65-84).
