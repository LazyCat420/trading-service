# Current state

Verified **2026-08-05** against `master@8182868` deployed to the NAS.

<div class="status-grid">
  <div class="tile ok"><div class="label">Tickers into analysis</div><div class="value">13</div><div class="note">was 0–1</div></div>
  <div class="tile ok"><div class="label">tool_playbook rows</div><div class="value">63</div><div class="note">purged from 4,948</div></div>
  <div class="tile ok"><div class="label">Playbook injection</div><div class="value">231 ch</div><div class="note">was ~120,000</div></div>
  <div class="tile warn"><div class="label">Agent tool-turn timeouts</div><div class="value">12</div><div class="note">new concern, see open items</div></div>
</div>

## Second cycle — `cycle-v3-1785985682`, and a correction to the first read

The first cycle after the debate rework showed a 33% bear win rate. The second,
with the `bull_defense` turn-budget fix also live, came in at **57%** (bear 4,
tie 2, bull 1).

| | baseline | cycle 1 | cycle 2 | pooled |
|---|---|---|---|---|
| Bear wins | 72–94% (288 debates) | 33% (9) | 57% (7) | **44%** (16) |

**So 33% was a small-sample low, not the new rate.** Pooled across both cycles
the bear takes 44% of 16 verdicts — clearly off the 72–94% baseline, and
consistent with a debate that no longer guarantees one side, but not the
step-change a single cycle suggested. Sixteen verdicts is still small; do not
quote a rate from this without more.

**A correction worth making explicitly.** I wrote earlier that "the floor was
never the binding constraint". That was too broad. This cycle produced:

```
UNH   BUY  64   HOLD_POLICY_BLOCKED_LOW_CONFIDENCE
```

The Board wanted to buy and the floor stopped it — the first time in these
cycles the floor has actually bound. Both mechanisms are real: `VNRX` decided
HOLD *at 74*, above the floor (the Board choosing), and `UNH` wanted BUY at 64
(the floor blocking). The original claim holds for the case it was made about
and not as a general statement.

**The defense turn is running:** 6 of 8 desks, against 8 of 11 when it had a
1-turn budget. The two without it (`ASIC`, `UNH`) still reached a verdict —
`UNH`'s judge ruled for the bull with no defense at all — which is the
fail-open path behaving as designed rather than stranding the desk.

**Artifact failures over the same window: 13 of 121 (10%)**, down from the
22–36% of 08-04/08-05 but not gone. That is the baseline the repair fix
(`83cb633`) has to beat; it was not yet deployed when this cycle ran.

## First evidence after the debate rework — `cycle-v3-1785978092`

The first cycle to run with the framing and the exit frame live (the
`bull_defense` turn-budget fix was **not** yet deployed, so this ran the
one-turn defense — 8 of 11 desks produced one anyway).

| | before | this cycle |
|---|---|---|
| Bear win rate | 72–94% (288 debates) | **33%** — bull 4, bear 3, tie 2 |
| Executions | 0 across the prior week | **1** (`C` BUY @ 70, `EXECUTE_BUY`) |
| Action mix | 94% HOLD | 8 HOLD, 1 BUY |

**Treat this as directional, not settled.** Nine judged debates is a small
sample against 288, one cycle cannot establish a rate, and the confidence
anchoring shipped the same morning is a second change inside the same window.
The bear win rate is still the cleanest signal available, because it does not
depend on the confidence scale at all.

**`POSITION_REVIEW` fired on exactly the held names** — LLY and LMT, both open
positions, both framed as keep-or-exit rather than as entry decisions. Both
resolved *keep*, which is a legitimate outcome; what matters is that the
question asked was the right one. No held name produced a SELL yet.

**One honest read of the frames:** `DATA_SUFFICIENCY` led 8 of 10 framings.
That is not the framer being monotonous — it is correctly detecting the damage
from open item 0. Missing valuation and fundamental artifacts become data gaps,
and the framer reads that as "is there enough here to have a view at all". If
the frames do not diversify once the vllm-shim is fixed, that is a finding
about the framer rather than about the data.

## Shipped 2026-08-05 (evening) — held positions are exit decisions

**A correction first.** An earlier note in this wave claimed SELL was
structurally unreachable because held names are never re-analysed. That was
wrong, and the query behind it was wrong: a `LIMIT 12` covered about two days
of cycles, not ten. The exit machinery works — all nine held positions carry
active watches, the watches fire (ALLY 08-03, HOOD and JPM 08-04) and are
evaluated hourly, and held names are re-analysed regularly. Two even executed
BUYs, at confidence 71 and 82.

**The real defect is semantic.** Zero SELLs in 14 days, because every re-look
of a held name reasons about *entry*:

> HOOD, held, re-looked 08-05: *"price remains below all SMAs with bearish
> MACD … we continue to wait for trend confirmation before **re-engaging**"* →
> HOLD at 52, on a position the book already owned.

The cause is an asymmetry in what the desk is told. The not-held branch of
`portfolio_context` states a hard **constraint** — "the bot cannot SELL what it
does not hold". The held branch stated only a bare **fact**: entry, P&L, days.
So the desk knew what it could not do when flat, and nothing about what it
could do when long. `HOLD` carries both "do not enter" and "keep the position",
and only the entry meaning was ever being reasoned about — producing a HOLD
that silently keeps a position the same paragraph describes as broken.

Fixed at the four places it matters: the held branch of `portfolio_context` now
states the decision frame (BUY adds, HOLD *keeps* as an active choice, SELL
exits and is correct when the opening thesis fails); `debate_frame` gains
POSITION_REVIEW at top priority, with ENTRY_QUALITY suppressed for held names
because entry quality is not a question about committed capital; and the Board,
the synthesizer and the delta tier each carry the same frame. All of them say
*judge the thesis, not the P&L* in both directions, and warn against
overcorrecting — an underwater position with an intact thesis is a KEEP, a
profitable one with a broken thesis is a SELL. The goal is a real exit
decision, not a sell bias.

**Historical note.** 167 of 176 SELLs (95%) were on tickers the bot did not
hold, and were policy-blocked after the desk had already spent ~1,243s each.
The not-held constraint correctly killed those; what it revealed is that
genuine exits were never being generated at all.

**How to tell if this worked.** Count SELLs on *held* tickers, not SELLs
overall — the old totals were mostly invalid shorts. Any exit should also carry
a thesis-based rationale, not a P&L one.

## Shipped 2026-08-05 (evening) — the debate rework

Traced backwards from ten HOLDs (write-up in the client's *Incidents*) and
found the confidence scale was **not** the binding constraint: `VNRX` decided
HOLD at confidence 74, above the floor. Two real defects, both fixed here.

**The debate was unfair, measurably.** The Bear runs with
`include_debate_context=True` and reads the Bull's thesis; the Bull gets no
debate context and never replies. `BULL_DEFENSE` lost its producer on 07-29 as
dead code — correct at the time, the tournament was the live engine — and the
linear bull/bear debate was restored to the live path on **07-30, one day
later, without its third turn**. Measured consequence: the Bear won **72-94%
of 288 debates**, and in a long-only book a bear win can only become HOLD.
The third turn is restored (`app/v3/agents/bull_defense.py`), and the judge no
longer lets an attack the Bull never had a chance to answer decide the winner
— those route to sizing instead.

**The debate was unconditional.** Every ticker got byte-identical prompts.
`app/v3/debate_frame.py` now derives the 2-3 live propositions for each desk
from artifacts already computed — SOLVENCY on a structural gate FAIL,
ENTRY_QUALITY when R:R is below the floor while the directional read is
constructive, DESK_DISAGREEMENT, VALUATION, DATA_SUFFICIENCY,
TREND_VS_REVERSION, CATALYST, and THESIS_DURABILITY as fallback. Deliberately
**deterministic**: no model call, no added cycle cost, and the trigger is
auditable after the fact. Verified against the two real cases — VNRX frames as
SOLVENCY, UBS as SOLVENCY + ENTRY_QUALITY.

Cost: one extra agent call per debating ticker (the defense).

**Known limitation.** The leverage gate fires at debt/equity > 4.0 against a
general-universe threshold, so a normally-levered bank (UBS at 4.52) opens a
SOLVENCY frame that a sector-aware gate would not. Both propositions still
reach the debaters, so nothing is lost — but the lead question is arguably
wrong for financials. Sector-aware gates belong to `decision_score`, not here.

**How to tell if this worked.** The bear win rate is directly measurable and
largely independent of the confidence anchoring shipped this morning: query
`debate_judge.winner` over `shared_desk`. A fair debate should land nearer
50-60% bear, not 72-94%. `proposition_verdicts` also makes it measurable
whether the debate answered the question it was given or argued past it.

**Measurement confound, stated plainly.** This morning's confidence-anchor
window (to ~08-12) is now confounded for the four debate agents, whose prompts
changed again. That was a deliberate trade: the debate defect outranks a clean
measurement of a secondary fix. The bear-win-rate metric above is unaffected.

## Shipped 2026-08-05 — the open-items wave

Six fixes landed in one branch (`fix/open-items-2026-08-05`), driven by the
client-side open-items list. Mechanisms, with the diagnostics that motivated
them:

**One firm-wide confidence scale (client open items 1+2).** Measured baseline
before the change, 348 desks over 14 days: every stage's mean confidence sat
at 57–66 — inside the 55–69 dead band below the calibrated execution floor of
70 (`final_decision` mean 60.2, stdev 15.2). The artifact schemas turned out
to be **validation-only** — `agent_runner` never serializes them into a
prompt — so anchoring them alone would have been a no-op. The operative
anchor is a "WHAT `confidence` MEANS" section added to nine agent prompts
(junior, quant, valuation, bull, bear, debate judge, delta, decision
synthesizer, regime), each defining the number as P(this stage's claim is
right over its horizon) with the Board's 80-90/70-79/55-69/<55 bands.
Schema descriptions mirror it via `_CONF_BANDS`. The floor of 70 is
untouched. **Do not edit these prompts again before a full measurement
window has passed** — the before/after comparison is the whole point.

**Regime fallback is distinguishable (client open item 2).** Diagnostic
first: recent regime artifacts are fully formed (313/313 carry every field),
so the CONTRADICTORY-87% dominance is the model's own emission, not a parse
fallback — classified once per cycle and copied to every desk (one label, one
confidence per cycle). The fallback paths are still made honest: the
orchestrator's initial and missing-field regime is now `UNCLASSIFIED`
(persona routing unchanged — unknown labels already fall back to Jane Street
with a warning), and a validator-coerced label stamps `regime_fallback: true`.

**Empty-response capture armed (open item 1 here).** See that item.

**Heartbeat orphan clear (open item 5 here).** `start_cycle` judges a stuck
`running` state by `updated_at` (stamped on every event emit) with a
15-minute threshold, not `started_at > 30min`.

**Per-ticker drop reconciliation (client open item 3).** Every ticker in the
fan-out now ends the cycle as either a real decision or an explicit
`v3_dropped_<ticker>` pipeline event with the reason (crash, abort sentinel,
no result, no price history). The noop HOLD/confidence-0 sentinel counts as a
drop, not a decision. The FDVV shape — 11 desks in, 10 decisions out, nothing
recorded — cannot recur silently.

**Watch trips report as `watch_trip` (client open item 7).** `list_cycles`
labels `wd-*` event groups `watch_trip` instead of letting them fall through
to `aborted`; the client badge is in trading-client.

## Verified working

**The playbook constraint holds.** `tool_playbook` sits at 63 rows behind
`uq_tool_playbook_natural_key`, down from 4,948. Injection measured live on the
running container:

| Agent | Injected | Lines |
|---|---:|---:|
| `v3_junior_analyst` | 231 chars | 3 |
| `v3_fundamental_analyst` | 223 chars | 3 |
| `v3_quant_analyst` | 204 chars | 3 |

**The writer upserts rather than accumulating.** `update_tool_playbook()` run
twice against production: row count held at 63, 61 rows had
`last_validated_at` refreshed, zero errors logged. All three signals are
required — row count alone cannot distinguish a working upsert from one that
raises on every row.

**Agents produce real artifacts again, and failures no longer abort a ticker.**
Measured over ~2 hours of cycle `cycle-v3-1785953340`:

| Signal | This cycle | Before the fix |
|---|---:|---|
| `no parseable artifact` | 2 | every agent, every ticker |
| `Circuit breaker tripped` | **0** | every ticker |
| Tickers `ABORTED` | **0** | every ticker |
| Agent tool-turn timeouts | 12 | not reached |

The two parse failures (`v3_fundamental_analyst` on UBER,
`v3_valuation_analyst` on NYT) were both absorbed by the single retry, so no
ticker was lost. That is the substantive change: before the fix a parse failure
was *guaranteed* on the retry too, so the breaker tripped every time and every
ticker aborted at the first agent. Occasional agent flakiness is normal; a 100%
deterministic failure was not.

Sample output from `v3_fundamental_analyst` on NYT, showing genuine work:

> NYT reported Q2 2026 earnings this morning that resolved the prior cycle's
> fundamental-vs-technical divergence in the bear's favor … guided Q3
> digital-only subscription revenue growth to just 12–15% vs Q2's 16.4%.
> `thesis_direction: BEARISH, confidence: 62`

**Gatekeeper failure degrades instead of deciding.** Caught live:

```
ERROR [PipelineService] Gatekeeper unusable (returned no parseable selection
('Agent failed: empty response from v3_portfolio_manager')) — degrading to
top 15 scorers
```

Before `b3d3d90` this exact fault ended the cycle green with 0 tickers.

**Worker identity is stamped on claims.** `claimed by worker=<name>/<sha>`.

**Agents complete with real verdicts**, not just parseable output. Sampled from
the verification cycle:

```
✅ CVS:  v3_fundamental_analyst → BULLISH @ 55% (9 turns, 529159ms)
✅ UBER: v3_fundamental_analyst → NEUTRAL @ 55% (5 turns, 203055ms)
```

After ~2 hours: **211+ pipeline events, 32 agent completions**, and the pipeline
has reached the **debate layer** (`v3_debate_judge` on SHOP), past the whole
research stack. The comparison that matters is the broken cycle from the same
morning — `cycle-v3-1785936600` produced **17 events total** and ended at the
gatekeeper.

## Not yet verified

**A full cycle has not been observed end to end since the fix.** The
verification cycle was still running at the time of writing, with no
`Saved analysis result` lines yet — those land when a ticker's whole pipeline
finishes. Selection, artifact generation, and per-agent completion are
confirmed. **Debate, trade execution, and cycle completion are not.**

**The cycle is slow.** 32 agent completions in ~2 hours, with individual agents
taking 203–529 seconds and **12** exhausting their tool-turn budget. At that
rate 13 tickers through the full 4+1 layer stack is a multi-hour run. Whether
this is a regression or was simply invisible while every agent failed instantly
is unknown — see [Open items](#open-items). It needs a baseline before anyone
calls it a regression.

## Shipped today

| Commit | Change |
|---|---|
| `b3d3d90` | Playbook natural key + dedupe migration, gatekeeper failure/refusal split, prompt cap, worker identity |
| `8182868` | Repeat the partial-index predicate so the upsert actually fires |
| `3653899` | Revive the dead startup readiness check; stamp `GIT_SHA`/`WORKER_NAME` at deploy |

> **`3653899` and later are pushed but NOT deployed.** The deploy was held
> because restarting the container kills the in-flight verification cycle,
> which was producing real analysis. Run `npm run deploy` once
> `pipeline_state.status` is no longer `running`. Until then the NAS is on
> `8182868`, and worker identity reads `<container-id>/unknown-build` rather
> than `nas-prod/<sha>`.
