# HANDOFF — simplification wave, confidence collapse, and the debate rebuild (2026-07-29)

**All work is merged to `master` (`c6869bc`). Nothing is pushed or deployed.**
9 commits from `49d5ed0`: +3,192 / −1,585 across 32 files.
Unit suite **1,943 passed**, 1 long-standing failure
(`test_parameter_tools::test_whitelists_grant_write_to_pm_and_board_only`) —
unchanged throughout, verified identical on a clean tree.

`trading-client` is at `9cf0e62`, also merged, also unpushed.

---

## The one-paragraph version

The user's hypothesis — "we made this too complex, that's why it keeps failing
even when we rebuild" — is supported, but the diagnosis needed correcting: it is
not too many features, it is **too many redundant carriers of the same fact,
each with a fail-open default**. A rebuild reproduces the seam topology, which
is why rebuilding has not helped. Four stages of deletion shipped with **zero
behavioural change**. Separately, the bot has been placing **zero trades** since
07-28; root cause found and fixed (a data-gap flag was acting as a near-binary
trade veto). The tournament debate was rebuilt as a scoreable probabilistic
panel rather than deleted, and is wired behind a parameter that defaults to OFF.

---

## Part A — the simplification wave (shipped, verified no-op)

`scripts/simplification_baseline.py --diff pre-stage1 post-stage4`:

```
STRUCTURAL (movement here is the point)
  app_loc                102,647 -> 101,416
  debate_coordinator.py    1,504 ->     246
  crossrepo_total             56 ->      42
  crossrepo_diverged          33 ->      21

BEHAVIOURAL (movement here needs a prediction)
  (none — a clean structural-only stage)
```

Same 93 decisions, same 7 BUY / 86 HOLD, same policy_action and provenance
distributions, same 15/109 desks clearing the floor.

**Stage 1 — deleted with no caller** (`a46c0a4`): the classic adversarial debate
engine (1,504 → 246 lines; only importer was a debug script), 11 config settings
across three unbuilt subsystems with **zero readers**, the `v3_bull_defense`
grants.

**Stage 2 — the cross-repo hazard** (`trading-client@9cf0e62`): the client had
**three** live routes into the shared database, including `ALTER TABLE` on
`positions` and `watchlist` from a 969-line fossil of the service's 3,868-line
migrations file. Both services migrated one database from divergent code;
whichever restarted last won. All three closed. **Deploy ordering is now
load-bearing: service migrates first, then the client connects.**

**Stage 3 — the jury veto** (`ded9860`): the *only* stated reason the tournament
was not deleted. Scored for the first time — it has **blocked zero decisions,
ever**. 19 vetoes total; the 4 since gates became observable all landed on
decisions already HOLD (killed at gate #3, veto is gate #10), and the 7
pre-instrumentation ones executed anyway with `policy_action = NULL`. The 7
vetoed-and-executed trades: mean **−0.04%** (n=7).

**Stage 4 — carriers** (`efb312f`): collapsed the duplicated tool-whitelist
cascade into one resolver, verified by differential test (all 20 agents +
unknown resolve identically). **Kept** the "duplicate" confidence floor after
measuring it: 5 of 35 executable decisions since 07-23 arrived with no
`policy_action`, two of them sub-floor BUYs only the second check would catch.

---

## Part B — the confidence collapse (root cause found, fix shipped, UNVERIFIED live)

Matched population (desks that produced a decision, technicals fresh ≤3d):

| window | n | mean | clearing 65 | clearing 70 |
|---|---:|---:|---:|---:|
| 07-14..19 | 147 | **77.1** | 140 (95%) | 123 (**84%**) |
| 07-26+ | 93 | **61.6** | 38 (41%) | 4 (**4%**) |

**Two hypotheses tested and REFUTED** — do not re-run these:
- *Dose-response* (more gaps → lower confidence): none. Mean is flat (~61) at
  every gap count.
- *Caveat density*: within-window r = −0.065 and +0.131. No relationship.

**What gaps actually do** is act as a near-binary gate: **0 gaps → 73% clear the
floor; ≥1 gap → 4%** (Fisher p=4.2e-09, OR=62) while the mean is identical
(61.0 vs 60.9). Gap presence went 57.8% → 90.2% of desks after `43a79fd`
(07-24) and `3ebdcf0` (07-26) made absence and staleness loud.

The prompt could not resist it: three lines say lower confidence, **zero** say
what sustains it, and `confidence` had **no schema description at all** while
the policy gate blocks every BUY/SELL below 70 on its value.

**Shipped** (`dcc00af`): a two-sided rubric in `_BOARD_COMMON` (70–79 is the
normal band for a sound thesis with ordinary gaps), a description on
`FINAL_DECISION_SCHEMA.confidence`, and `[BLOCKING]/[MATERIAL]/[MINOR]`
severities on `data_gaps` (untagged → MINOR). The **NONE ON FILE** blocks now
say a missing baseline should sink a thesis that *rests* on it.

**Two measurement traps, recorded so they are not re-hit:**
1. *Population*: the raw `shared_desk` average appears to go **up** (47.4 →
   60.9) because the before-window holds 109 orphan desks at mean confidence 7.5.
2. *Confound*: the floor moved 65→70 on 07-26. Re-cut at a fixed bar — the
   collapse holds at both.

---

## Part C — the tournament rebuild (built, wired OFF, unscored)

New: `app/cognition/debate/panel_math.py` (I/O-free, so the part that decides
what the panel *says* is testable without a model),
`app/cognition/debate/probabilistic_panel.py`, `scripts/score_panel.py`.

Four analysts on **disjoint** evidence slices, each emitting
`P(up >1% over 7 sessions)`, one revision round on peers' *reasoning*, pooled by
confidence-weighted logit averaging. Added a `Positioning` filter category and
widened the packet — `valuation_report` was never in it, and `Momentum_Quant` /
`Volatility_Quant` had been reading the identical single fact.

**The scorer already produced a result on the EXISTING tournament** (n=98 since
07-01, the first time it has ever been scoreable):

| | Brier |
|---|---:|
| tournament | **0.3090** |
| constant 0.5 | 0.2500 |
| base rate (p̄=0.347) | **0.2266** |

Worse than a coin flip, with resolution 0.0165 — no discrimination.

`DEBATE_ENGINE`: `0` = tournament (**default, nothing changed in production**),
`1` = panel, `2` = panel with shared evidence (the ρ=1.0 control). It gates the
**call**, not the rendering — the old `TOURNAMENT_DEBATE_MODE` shadow branch was
measured to save **zero** tokens because the debate ran regardless.

---

## What the next agent should do, in order

### 1. Verify Part B on a live cycle — this is the blocker for everything else

```bash
python scripts/simplification_baseline.py --label pre-partB   # if not already taken
# run a real cycle in the deployed container
python scripts/simplification_baseline.py --label post-partB
python scripts/simplification_baseline.py --diff post-stage4 post-partB
```

**Part B is the one stage where "no behavioural change" means FAILURE.** The
prediction, written before shipping: MINOR-only desks should clear the floor at
a rate near the 73% that zero-gap desks manage today, while BLOCKING-gap desks
stay suppressed, and executable BUYs recover from 0/44 **without the floor
moving**.

If confidence does *not* recover, the low confidence was honest — report that
and move to input quality (see item 4), **do not lower the floor**. It is
calibrated at n=859 (conf<70 −1.78%, ≥70 +3.69%).

### 2. Score the panel

```bash
# set DEBATE_ENGINE=1, run cycles, then:
python scripts/score_panel.py --since 2026-07-29
```

**The bar is not the tournament** — it is already known to be noise. In order:
the base rate (the real null), then **self-consistency** (same model, full
packet, k samples, `p = fraction bullish`), which still needs writing and is the
bar that decides whether the panel ships. Then set `DEBATE_ENGINE=2` for the
ρ=1.0 control: if the panel beats self-consistency but not that, the gain is
ensembling, not information asymmetry.

**Report Murphy's decomposition, not the total.** Resolution is the number that
matters — this system's standing finding is that it can spot its own bad
decisions but cannot pick winners, and a panel that only improves reliability
has bought nothing. Needs **n≈100**; below ~50 the noise band is ±0.148.

Null result, agreed in advance: if the panel cannot beat self-consistency,
delete the debate and take the ~203s/ticker.

### 3. Push and deploy

Nothing is pushed. `trading-service@c6869bc`, `trading-client@9cf0e62`.
**Deploy the service before the client** — the client no longer creates schema.

### 4. Fix the real data decay (independent of the above)

26% of analysed tickers have stale or missing technicals; only 666 of 2,728
tickers are refreshed. In the high-confidence window it was **100%** fresh. This
is worth ~2 of the ~15 confidence points and is a genuine input problem: the
analysed universe should be a subset of the refreshed set.

### 5. Loose ends

- A worktree is still checked out at
  `trading-service/.claude/worktrees/fidelity-followup` (it was the session's
  cwd, so it could not remove itself). `git worktree remove` it and delete
  branch `worktree-fidelity-followup`; everything in it is merged.
- `docs/JURY_VETO_SCORECARD_2026-07-29.md` and
  `docs/BOARD_HOLD_DECOMPOSITION_2026-07-29.md` are the two evidence write-ups.
- The tournament's equation auto-save (`tournament.py:387-392`) writes
  non-executable stubs into `quant_equation_library` that the nightly lab then
  has to compile. Not touched. If the panel ships, this goes with the tournament.

---

## Standing caveats

- **Whether today's low confidence is correct is unanswerable for ~2 weeks.**
  Post-fidelity resolved BUYs are n=26 and span only 07-20..21 — *before* the
  collapse.
- **The V3-vs-pre-V3 gap is time-confounded.** pre-V3 +3.46% (n=688) vs V3
  −0.48% (n=74) is significant (Welch p<0.0001) but the eras do not overlap, and
  there is no pre-V3 path to revert to — `pipeline_service.py:1289` has no branch.
- **The whole system trails always-long by ~1%** (+3.49% vs +4.46% net). None of
  this session's work claims to change that; the claims are cost, seam count,
  and non-regression.

## Method note

Every finding here came from running something. Three of my own hypotheses were
refuted by the data (dose-response, caveat density, and the
unshortable-SELL-rename explanation for the HOLD rate), a population trap nearly
inverted the confidence finding, and a differential test that reported "every
count is exactly half" turned out to be a missing `tool_schemas.json` in the
worktree rather than a refactor bug. Read `git log` bodies — they carry the
measurements, including the refutations.
