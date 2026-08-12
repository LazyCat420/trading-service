# HANDOFF — Open Item 46: a HOLD on a name we OWN (2026-08-12)

Shipped and deployed as **`e331872`**. The container was verified to carry it
(`app/v3/wake_pool.py` present, the new labels importable inside the running
image) — a green suite is not a shipped change.

> **The full write-up is chapter 56 in `trading-client/documentation/`**, served
> at <http://10.0.0.16:8888/documentation>. That is where this service's
> chapters live by standing agreement — see `CLAUDE.md`. This file is the
> in-repo handoff: what changed here, which invariants it discovered, and what
> the next session must not re-derive.

---

## What the item said, and what was actually wrong

Filed: `hold_reason` labels a `HOLD` on a name the book owns as `WATCH` — "the
thesis is constructive; the desk is not entering *here*" — about capital already
committed. True: **26 of 28 labelled HOLDs on held names read `WATCH`**, the
other 2 read `AVOID`, which is wrong the same way.

The label was the smaller half. Measured read-only over **149 desks / 132 HOLDs
/ 33 held**, since 2026-08-08:

| input `classify_hold` splits on | held = False | held = True |
|---|---|---|
| candidate pool present | 90 / 115 | **2 / 33** |
| bear `NAMED` or `DECLINED` | 62 / 71 | **2 / 23** (`NOT_ASKED` 21) |
| bear won the debate | 54 / 78 debated (**69%**) | **0 / 26 (0%)** |
| `decision_score` band = `AVOID` | 25 / 115 | **0 / 33** |
| signal 3 (`thesis_direction`) | 0 firings | 0 firings |

**Every discriminating input was absent on the population the new branch was
for.** A position-aware split alone would have been a constant function
(replayed: `KEEP` 31 / `EXIT_SIGNALLED` 2, and both of those came from the input
that already produced `AVOID`). So the inputs were fixed first.

---

## What is live right now

### 1. Signal 3 was dead code, and is repaired

`classify_hold` read `thesis_direction` from `final_decision` (**0 of 141**
desks carry the key), `trade_decision` (**0 of 105**), and `decision_synthesis`
— **which is not an artifact this desk produces**. The field is declared on
`fundamental_report` (111/111) and `quant_report` (105/105), see
`artifacts.py:118` and `:279`.

`_DIRECTION_CARRIERS` now lists the research artifacts after the decision ones.

> **Blast radius, measured BEFORE the change:** 6 of 132 HOLDs gain the signal,
> all on unheld names, and exactly **1** label flips `WATCH` → `AVOID`. This
> repairs a dead path. It does not move the held branch and was never going to.
> Recorded so the repair is not later mistaken for a result.

The test that missed it hand-built its own desks.
`test_thesis_direction_carriers_exist_in_the_live_schemas` now asserts the
carrier list against `artifacts.py` instead of against a fixture.

### 2. `app/v3/wake_pool.py` — the bear can now be asked on a name we own

`NOT_ASKED` means *there was no pool*. A Watch Desk wake names one ticker and
bypasses discovery, and the names the desk owns are exactly the ones it re-looks
at on a wake. So the substitute axis — which `hold_reason` calls its **primary**
axis — was unavailable on precisely the population where an exit is the decision.

A held re-look with no pool now borrows the ticker list from the desk's most
recent **full** cycle. One indexed read, no model call, no discovery re-run.

- **48-hour window.** A full cycle lands every few hours; the newest pool is
  normally <12h old.
- **Screen numbers are dropped.** `chg`/`rvol` are intraday and these rows are
  not. Re-rendering yesterday's relative volume under a current-looking header
  is a freshness defect wearing a data table.
- **`substitute_ask_skipped` is written on every path including success**, so
  "no pool existed", "the pool was stale" and "the bear ignored the question"
  stop pooling into one `NOT_ASKED`. That conflation is why this was invisible.

> **HELD-ONLY, deliberately.** Unheld pool-less desks are untouched and are the
> **control group**: if held `NAMED`/`DECLINED` rises while the unheld
> pool-less rate does not, the pool is what did it. Widening this to every wake
> buys coverage and loses the comparison. Do not "improve" it by removing the
> guard — `test_the_wake_pool_only_fires_for_HELD_names` pins it.

### 3. `classify_hold` branches on position state, three ways

| state | vocabulary | question |
|---|---|---|
| `held is False` | `WATCH` / `AVOID` | should the desk **enter**? |
| `held is True` | `KEEP` / `EXIT_SIGNALLED` | should the desk **stay in**? |
| `held is None` | `UNKNOWN_POSITION` | the desk could not tell |

`EXIT_SIGNALLED` and **not** `SELL_PROPOSED`: nothing proposed a SELL — the
emitted action was `HOLD`, and there were **zero SELL actions across 149
desks**. `DECLINED` does not rescue a broken thesis on a held name, which is the
one place the branches genuinely disagree rather than rename: exiting to **cash**
is always available on a long-only book, so "no better name exists" says nothing
about whether *this* position should still be owned.

### 4. The exit ratchet — measured, deliberately NOT moved

`_attach_exit_shadow` records the counterfactual at **both** decision exits and
gates nothing. See the invariant below.

### 5. `scripts/hold_wall_report.py` gained an OPEN ITEM 46 section

```bash
python3 scripts/hold_wall_report.py --since 2026-08-12
```

- **LEAK** — wrong vocabulary for the state. Was **28 / 132**. Target **0**.
  Reaching 0 proves the label is *honest*; it does not prove it says anything.
- **SPREAD** — the held branch's distribution, with the dominant-label share.
- **INPUT AVAILABILITY** — the census table above, regenerated on demand.

> **SPREAD is the number that decides whether this work mattered.** If it stays
> ≥90% `KEEP` once the wake pool has run for a few days, the label is cosmetic
> and must be reported as such, not sold as a measurement improvement.

---

## Invariants discovered — do not re-derive these

1. **`cycle_metadata["portfolio_context"]` is a formatted PROSE STRING**
   (`orchestrator.py:3661` and `:3689`). `portfolio_context.get("held")` raises
   `AttributeError` on a `str`, and `_attach_hold_reason`'s blanket `except`
   swallows it — **the label vanishes instead of failing loudly**. The
   structured copy is `cycle_metadata["position"]`, written at `:3682` for
   exactly this use. A proposed plan reached for the prose key; it would have
   silently disabled the feature it was adding.

2. **`held` is a TRI-STATE, not a boolean.** It is absent when the portfolio
   fetch raises at desk-build time (~1 desk in 149). Coercing that to "not held"
   is the 07-23 defect that sent three unheld SELLs to the executor as silent
   no-ops. `resolve_held` returns `True` / `False` / `None`, and truthiness
   coercion is banned and pinned by test.

3. **The confidence floor is SYMMETRIC across entry and exit.**
   `_apply_policy_gates` runs `if confidence < floor:
   HOLD_POLICY_BLOCKED_LOW_CONFIDENCE` *before* its `if action == "BUY"` branch,
   so leaving a position needs as much conviction as opening one — on a book
   where doing nothing is the default. Held names are **not** confidence-starved
   (mean 66.8, **24 of 33 ≥ 70**, vs unheld 56.2 and 34 of 108). The floor is
   not what blocks exits; **the desk never proposes one**.

   > The shadow deliberately does **not** count "SELLs the floor blocked" —
   > there are none, and that number would be a constant 0 masquerading as
   > evidence. It counts held names whose own label says an exit signal exists
   > and whose confidence would clear an exit-side floor.

4. **A consumer count does not transfer between labels.** A plan cited "9
   enumeration sites" for `hold_reason`; **5 of the 9 named files contain zero
   occurrences**, and `trading-client` contains zero repo-wide. That nine-site
   list belongs to the *outcome* label (`HOLD_AVOIDED_DECLINE`). `hold_reason`'s
   real footprint is three production files and **no consumer** — it lives
   inside `analysis_results.result_json`, with no column and no route.

---

## Open items — filed as 47, 48, 49 in the served open-items chapter

- **47 · The bear wins 0 of 26 debates on held names, 69% on unheld.**
  p ≈ 0.31²⁶ ≈ 1.3 × 10⁻¹³ under the unheld base rate. **`61f1e1d` (2026-08-05,
  "a held position is an EXIT decision") already tried to fix this from the
  prompt** and was live across the whole measurement window. The prompt landed;
  the outcome did not move. **Re-measure after the wake pool has run for several
  days** before concluding the framing is at fault — the pool removes one
  confound.
- **48 · The exit ratchet** (invariant 3 above). Do not move the floor on this
  item alone: the confidence *scale* is itself in shadow, and moving a threshold
  on top of an unvalidated scale makes both unattributable.
- **49 · The decision vocabulary cannot express the most common risk action.**
  `artifacts.py` pins `"enum": ["BUY", "SELL", "HOLD"]` in **three** schemas.
  No `TRIM`, no `ADD`, no `TIGHTEN_STOP` — on a held name the desk cannot say
  "cut it in half". This is why item 46's label defect kept regenerating, and it
  is the blocker for any risk-deliberation layer.

  Measured in the same pass: **`DEBATE_ENGINE = 3` switches every debate engine
  off**, so `cognition/debate/probabilistic_panel.py` — which pools calibrated
  probabilities in logit space with a revision round — ran on **1 of 149 desks**.
  `quant/trial_registry.py` has **no importers anywhere**; `stat_gates`,
  `residual_alpha`, `execution_costs`, `sizing_bracket` and
  `cognition/verification/sufficiency_gate.py` are unreachable from the cycle.
  This service owns a pre-registration registry, a multiple-testing gate and a
  sandboxed backtest runner, and the trading cycle reaches none of them.

---

## Ownership warning

`.worktrees/wt-confidence-bear-fix` (branch `fix-confidence-bear`, **behind
master**) holds an **uncommitted 18-line change to `_apply_policy_gates`**
lowering the floor to 65 for R:R ≥ 3.0 — symmetric across BUY and SELL, and
contradicted by the floor-70 calibration. Resolve that ownership before editing
that function.

---

## Test baseline

- `pytest tests/unit` → **3860 passed**. The **3** failures in
  `test_tool_repair_on_the_live_path.py` are **pre-existing on `master`**
  (confirmed by running them there) and belong to the `repair_tool_arguments`
  open item.
- `test_reddit_collector.py` and `test_reddit_purge_rss_comments.py` **cannot be
  collected locally** — `ModuleNotFoundError: feedparser`, which is
  container-only. Run with `--ignore` for both.
- **26 new tests, all sabotage-verified red** across 7 independent sabotages
  (dead carriers restored, held branch deleted, fail-closed branch deleted,
  held+`NAMED` neutered, wake pool emptied, self-ticker exclusion removed,
  held-only guard dropped). A test that cannot be made to fail proves nothing.

## Next step — CORRECTED 2026-08-12 after reading the July handoffs

An earlier draft of this file said "run the probabilistic panel in SHADOW —
one parameter and one logging call". **Both halves of that were wrong**, and
the corrections came from documents already in this repo:

1. **`DEBATE_ENGINE = 3` is a measured retirement, not an oversight.**
   `HANDOFF_tournament_retired_2026-07-29.md`: the tournament cost **28.2% of
   ALL pipeline tokens** and **374 s/ticker**, and lost on every channel it was
   scored on — selection indistinguishable from the free `quant
   thesis_direction` (−0.822pp vs −0.771pp, both p≈0.35), 6.5× worse on the
   removal channel that actually pays, chi2=16.63 p<0.0001 redundant with the
   quant, and Brier **0.3090** against a base rate of **0.2266** (n=98). It was
   retired on evidence. Do not reopen it as if it were an accident.

2. **The panel is NOT the tournament, and is genuinely unmeasured.** The 0.3090
   above scored the *tournament* — `probabilistic_panel.py` sits at
   `DEBATE_ENGINE` 1 (and 2 for the ρ=1.0 control) and has **never been the
   default**; `tournament_result` appears on **1 of 149 desks**. So the
   experiment is real. But it is four analysts plus a revision round — the same
   order of cost as the thing that was retired for costing 28.2% of tokens.
   **It is not "one parameter and one logging call".** Budget it.

3. **The decisive baseline is already specified, and is cheaper than the
   panel.** `scripts/score_panel.py` names it: **self-consistency** — the same
   model, the FULL packet, k independent samples, `p` = fraction bullish, no
   debate and no partition — and states that "the literature's central finding
   is that most debate systems lose to this at 2-3x the tokens. **This is the
   one that decides whether the panel ships.**" It has never been run.

**So the ordered next step is: run self-consistency FIRST.** It is strictly
cheaper than the panel, it is the baseline the panel must beat to be worth
having, and if it wins there is nothing to build. Only if the panel beats
self-consistency — and then also beats the ρ=1.0 shared-evidence control, or
the gain is ensembling rather than information asymmetry — is a debate engine
worth its tokens.

The scorer, the baselines and the control are all already written. What is
missing is a run.
