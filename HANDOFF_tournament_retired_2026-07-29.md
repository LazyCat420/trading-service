# The tournament debate is retired — DEBATE_ENGINE defaults to 3 (no debate)

**Measured and shipped 2026-07-29.** Frees **28.2% of all pipeline tokens** and
**374 s/ticker**.

The tournament was the single largest cost centre in the pipeline and had never
been scored against the alternative that costs nothing. It has now been scored on
every channel it could plausibly earn its keep, and it lost each one — most
decisively to a signal already sitting on every desk for free.

---

## The cost, measured

```
v3_tournament_debate .... 77,698,387 tokens over 347 runs
all agents .............. 275,918,919 tokens
                          -> 28.2% of ALL pipeline spend
avg per run ............. 223,915 tokens, 373,977 ms  (374 s/ticker)
fallback rate ........... 14/413 = 3.4%   (so it really did run)
winning_side ............ bear 245 · bull 115 · skipped 20 · veto 19 · fallback 14
```

## The benefit, measured

Population: 137 desks with a *decided* tournament (bear/bull) joined to a
non-degraded `decision_outcomes` row. `DEGRADED_ARTIFACT` excluded — those are
pipeline failures, not market calls.

**1. Selection — indistinguishable from free.** On desks the board actually
traded, does the verdict rank realized P&L?

| signal | separation | p | marginal cost |
|---|---:|---:|---|
| tournament (bull vs bear) | **−0.822pp** | 0.34 | 28.2% of tokens, 374 s/ticker |
| **quant `thesis_direction`** (BULLISH) | **−0.771pp** | 0.35 | **zero — already on every desk** |

Same effect size, same non-significance, one of them free.

**2. Removal — the free signal is 6.5× better.** `parameter_store.py:94-97`
records that *"the gain comes from REMOVING trades"*, so this is the channel that
matters most. Among desks the board HELD (negative P&L ⇒ the skip was right):

```
quant-BEARISH  & board HOLD ....... -1.851%  (n=14)
tournament-bear & board HOLD ......  -0.285%  (n=29)
```

**3. Not incremental.** Conditioned on the quant's direction, the verdict adds
nothing where the desk already has a view:

```
within quant=BEARISH:  bear +0.791% (n=36) vs bull +0.462% (n=13)  diff +0.33pp  p=0.84
```

The apparent effects live only in the `quant=NEUTRAL` (+3.07pp, p=0.022) and
`quant=BULLISH` (−2.30pp, p=0.071) cells, at n=13 per side. Three subgroups
tested ⇒ Bonferroni puts the best of them at p≈0.066. That is noise.

**4. Strongly redundant.** `winning_side` is highly dependent on the quant's
`thesis_direction` — chi2 = **16.63, p < 0.0001** (36/49 quant-BEARISH desks get
tournament-bear; 33/47 quant-BULLISH get tournament-bull). It largely re-derives
a signal the desk already has.

**5. Its own probability is worse than useless.** Brier **0.3090** vs base rate
0.2266 and a constant-0.5 at 0.2500 (`scripts/score_panel.py`, n=98).

**6. The jury veto has blocked zero decisions, ever**
(`docs/JURY_VETO_SCORECARD_2026-07-29.md`). That veto was the *sole stated reason*
the tournament was shadowed rather than deleted.

### The one result that looked like evidence, and isn't

`HANDOFF_harness_audit_2026-07-29.md` cites the tournament as "directionally
discriminating at p=3.2e-09" (bear-win → HOLD 74% vs bull-win 30%). Reproduced
here: bear → 37.2% HOLD, bull → 6.8% HOLD. But that association is between
`winning_side` and the **board's action** — it measures that the board *listens*
to the verdict, not that the verdict is *right*. Combined with finding 4, it is
the redundancy channel: the board is being moved by a re-statement of the quant.

### Honest limit

At n=137 this **cannot prove the tournament is harmful.** It shows no measurable
benefit on any channel against a certain, large, measured cost. That is the
standard being applied: *don't pay 28% of the budget for an effect you cannot
detect and can get for free.*

---

## What changed

- `app/services/parameter_store.py` — `DEBATE_ENGINE` gains value **3 = no
  debate**, and **3 becomes the default** (`max_value` 2 → 3). Engines 0–2 remain
  selectable, so the comparison can be re-run at any time. Flip back to 0 to
  restore the old behaviour. The full measurement is recorded in the comment
  block above the ParamSpec.
- `app/v3/orchestrator.py` — `_execute_tournament_debate` resolves the engine
  **before emitting anything** and returns immediately on 3.

Three deliberate design choices:

1. **Engine 3 does not synthesize a verdict.** Deriving a `winning_side` from the
   quant would hand the board a computed number dressed as a debate outcome —
   the same failure mode as the 171-of-305 invented RSIs. It appends **no**
   `tournament_result` and **no** `debate_judge`.
2. **The fail-open now lands on 3, not 0.** With the default at 3, falling back
   to the tournament on a parameter-store hiccup would let a transient error
   silently resurrect 28.2% of spend. A fallback must land on the chosen
   behaviour, not the most expensive one.
3. **The skip is stamped.** A zero-token `outcome="SKIPPED"` telemetry row keeps
   *"did we actually stop paying for it?"* a queryable question. Without a
   stamped field the change would be unfalsifiable — the repo's own lesson.

**Bull / bear / bull-defense still run.** They are a separate phase
(`_queue_debate_phase`, `orchestrator.py:965`), not part of the tournament, so
the board keeps its adversarial views. Only the 4-persona pitch/head-to-head/jury
layer is gone.

## Absence tolerance (the part that could have broken trading)

Every consumer was verified against a desk with no `tournament_result`:

| consumer | behaviour | why safe |
|---|---|---|
| `_apply_policy_gates` jury veto | no phantom block | `getattr(desk, "tournament_result", None) or {}` → `.get("vetoed")` falsy |
| `shared_desk.get_compressed_context` | section omitted | guarded by `if tournament:` |
| `whiteboard` summary | no row written, none read | `if not rows: return ""` |
| `contradiction_shadow` | **still detects** | `final_decision` + both `thesis_direction`s still populate the sentiment map — verified, 1 contradiction caught on a quant/fundamental conflict |
| `quality_scorer` | rubric unused | keyed by artifact type, only scored when present |
| offline `scripts/score_*`, `agent_*` | unaffected | historical rows persist; new cycles simply add none |

## Two invariants will react — both correctly

- **`AGENT_BURNS_TOKENS_WITHOUT_RESEARCH`** (`COST_NO_RESEARCH_TOKENS = 150_000`)
  existed to surface precisely this cost (the tournament at 242k/call with
  `loops=1.0`). It is now satisfied structurally rather than by threshold.
- **`DECISION_DISTRIBUTION_DRIFT`** (`DRIFT_MIN_SHIFT_PCT = 25` vs a 150-decision
  baseline) **is expected to fire once.** The board leaned on the verdict hard
  (bear → 37.2% HOLD vs bull → 6.8%, and bear won 68% of decided tournaments), so
  removing it genuinely shifts the HOLD distribution. That firing is a **true
  positive for an intentional change**, not a fault — it should be absorbed as
  the baseline rolls forward, exactly as the 07-29 ramp was. Do not mute the
  check; the comment there warns that muting is how an observability layer dies.

## Verification

- `tests/unit/test_debate_engine_off.py` — 13 new tests: the default and range,
  fail-open lands on 3, engine 3 emits no artifact and no `winning_side`, the skip
  precedes the `starting` emit (otherwise the office UI holds an orphaned
  `running` node), the skip is stamped, and five absence-tolerance checks.
- Two tests in `tests/unit/test_probabilistic_panel.py` were updated rather than
  deleted: they pinned `default == 0` and `_engine = 0`, which were correct while
  0 was the default. Both now pin the *invariant* ("fail open to the default")
  with the reason recorded inline.

## Next — verify the saving, then decide about Wave 2

1. **Confirm the 28.2% is actually saved.** After one cycle:
   `select agent_name, outcome, count(*), sum(token_usage) from v3_agent_telemetry
   where cycle_id = '<new>' group by 1,2` — expect a `v3_tournament_debate /
   SKIPPED` row at 0 tokens and a cycle total ~28% below the trailing average.
   If tokens did not drop, the retirement did not take.
2. **Then reconsider whether to build Wave 2 at all** (VaR/ES, Kalman beta,
   ADF-as-gate — scoped in `~/.claude/plans/please-look-at-this-quiet-nebula.md`).
   Those are *selection* improvements, and two independent measurements now say
   selection is near its ceiling: confidence carries no ordering information
   above the 70 floor (70-79 → +2.49%, 80+ → +2.60%), and
   `parameter_store.py:94-97` already records "high confidence beats the null" at
   p=0.215. The same reasoning that retired the tournament applies to anything
   else that cannot show an effect — measure first, build second.
