# Current state

Verified **2026-08-05** against `master@8182868` deployed to the NAS.

<div class="status-grid">
  <div class="tile ok"><div class="label">Tickers into analysis</div><div class="value">13</div><div class="note">was 0–1</div></div>
  <div class="tile ok"><div class="label">tool_playbook rows</div><div class="value">63</div><div class="note">purged from 4,948</div></div>
  <div class="tile ok"><div class="label">Playbook injection</div><div class="value">231 ch</div><div class="note">was ~120,000</div></div>
  <div class="tile warn"><div class="label">Agent tool-turn timeouts</div><div class="value">12</div><div class="note">new concern, see open items</div></div>
</div>

## 2026-08-06 (night) — the Jetson measured, and the first real cycles run

Run before putting anything near production, in this order: benchmark the box,
answer the 42-day silence, then run a real cycle and read its log.

### The box is healthy — n=10 × 4 arms, interleaved, real replayed prompts

| arm | non-empty | valid artifact | median (warm) | cold |
|---|---|---|---|---|
| `chat` | 10/10 | **10/10** | **16.9 s** | 15.5 s |
| `agent` | 10/10 | 7/10 | 75.8 s | 230.5 s |
| `agent+tools` | 10/10 | 8/10 | 67.4 s | 24.8 s |
| `agent-nominp` (known-bad) | **0/10** | 0/10 | 1.4 s | 1.4 s |

Second independent n=10 on the same day, and `/chat` is **20/20 valid across
both**. The `minP` control is **0/20** — deterministic, not flaky.

The `/agent` losses are a *different* failure from the one that was fixed:
`NON_JSON` at 24–30k characters and 205–230 s, i.e. the model narrating instead
of emitting its artifact. Empty responses are gone; artifact discipline on
`/agent` is not solved. That is an argument for the transport rule, not
against the box.

### What actually happened on 2026-06-25

The documented story — "12,720 Jetson calls, then the box went dark" — was read
off `llm_audit_logs`, and it is **an artefact of the desk stopping, not the
Jetson failing**:

* `dgx_spark` stops on the **same day** in the same table — and that box has
  served every cycle since.
* `cycle_run_summaries` shows **zero cycles between 06-21 and 07-13**. A
  23-day desk-wide outage, matching the audit gap exactly.
* The roles that used the Jetson — `ticker_validator` (2,515 calls),
  `smart_janitor` (1,891), `summarizer_news` (1,512), `watchlist_curator`
  (780), `voice_data_janitor`, `narrative_curator` — **no longer exist**. They
  are the same names still hardcoded in the keyword routing list, which is why
  that list "matches no live agent name" (open item 1a).

So nothing on the box needs fixing before it can be trusted. **The workload was
retired; the box was never broken.** What is missing is a role, and that is a
deliberate decision, not a repair.

### The cycles

Two real cycles, queued as `START_CYCLE` on `v3_system_commands` so the NAS
worker claims them (a local process would be an equal claimant — see the
2026-08-05 outage).

* `cycle-v3-1786072624` — the gatekeeper chose nine tickers and **the cycle
  analysed one**. A regression shipped hours earlier; full write-up in
  [Incidents](#incidents), fixed in `fd62533`.
* `cycle-v3-1786074021` — gatekeeper chose eight, log reads *"Processing 8
  tickers"*. Selection intact.

The first-ever gatekeeper shadow row came from that second cycle, with
`primary_elapsed_ms = 3605` — a real number where every row would have recorded
0 before `f073679`. Its outcome was `AGENT_ERROR`, from a transient model-probe
timeout; that path now degrades instead of failing (`563b9ab`), and the box was
measured innocent: `/v1/models` answered **0/30 probes over 2 s**, median 9–10
ms, idle *and* under 8 concurrent generations.

## Shipped 2026-08-06 (late) — the wave was verified, and it was not all correct

The five commits below shipped green at 3,048 tests. Re-verifying them
behaviourally — driving the code instead of asserting on its source text —
found **two defects in the gatekeeper shadow**, both of which would have
corrupted the n≥10 measurement the Jetson decision is waiting on rather than
failing loudly:

* the shadow fired on the **degraded fallback**, comparing the second box
  against the scoring engine's top-N while the row claimed a gatekeeper
  primary — reachable by three routes, four of which occurred on 2026-08-06;
* `chat_toolless` returned no `execution_ms`, so every gatekeeper row would
  have recorded **`primary_elapsed_ms = 0`**.

Both fixed. The dispatch is now `pipeline_service.maybe_shadow_gatekeeper()`,
which returns whether it fired so its refusals can be asserted.

Also corrected: prism does **not** inject `minP=0.05` per endpoint. It applies
its agent defaults inside `if (agent)` in the shared
`prepareGenerationContext`, so the trigger is the **`agent` field in the
payload**. `/chat` was safe only because `chat_toolless` omitted that field —
and since the transport rule routes every tool-less role through it, that
accident was guarding most of the desk. `chat_toolless` now sends `minP`
explicitly, from the same `min_p_for` decision function as `/agent`.

Live acceptance (`scripts/verify_shipped.py`, new): the Jetson answers with
`min_p=0.0` **2/2 non-empty at 2,776 ms**, still returns **0/2** with the field
omitted, and the production `/chat` helper answers in **535 ms**. Suite now
3,105. Full account in [Verifying the 2026-08-06 wave](#verification).

## Shipped 2026-08-06 — the agent's tool declaration picks its transport

`base_agent.transport_for(enable_tools, agent_tools)`: declared tools →
`/agent`, none → `/chat`. The transport used to be hardcoded per call site, so
the declaration and the route could disagree with nothing able to reconcile
them — the gatekeeper's prompt instructs it to call `get_parameters`, its
whitelist carries 14 tools, and its call site passed `enable_tools=False`.

`/chat` is **not** a tool-less endpoint. Prism's `ChatRoutes` honours
`functionCallingEnabled`/`enabledTools`, injects tool schemas into the system
prompt, and executes calls through `ToolOrchestratorService`. The real
difference is *who decides*: opt-in on `/chat`, server-side policy plus a
forced agentic loop on `/agent`. So this routes on the declaration rather than
preferring one endpoint outright.

Fail-safe direction is deliberate: no input combination reaches `/agent`
without a non-empty tool list, and every ambiguous case goes to `/chat` only
when tools are absent. A tool-using agent on `/chat` silently loses its tools;
a tool-less agent on `/agent` is merely slower.

Also shipped: the gatekeeper is now **shadowable**. It does not run through
`agent_runner`, which was the only place that dispatched a shadow, so
`MODEL_SHADOW_AGENTS=v3_portfolio_manager` previously did nothing at all —
silently. Every box comparison so far describes `v3_regime_engine`, a role
whose tools show zero calls in 60 days; the gatekeeper is the tool-declaring
case no measurement could reach.

## Shipped 2026-08-06 — `scripts/jetson_benchmark.py`

Answers two questions that were previously re-derived from scratch every
session, and persists both to `box_benchmark_runs` so they accrue instead of
resetting.

**`--phase inventory` — what the box has actually processed.** vLLM's counters
are since *process start* and do not survive a restart, so the durable history
has to come from the database. First run:

| source | volume | window |
|---|---|---|
| `llm_audit_logs` (jetson) | 12,720 calls | 2026-06-06 → **06-25**, then nothing |
| `v3_agent_telemetry` (`provider='vllm'`) | 0 | ever |
| `model_shadow_runs` (jetson) | 21 SUCCESS / 1 error | 08-04 → 08-06 |
| vLLM counters | 127 requests, 1.52M prompt tok | 50.1h uptime → **2.53 req/hr** |

**`--phase reliability` — transport arms on replayed real prompts.** The corpus
is `model_shadow_runs.system_prompt/user_prompt`, i.e. prompts the desk
actually sent; those columns exist so a replay costs seconds instead of an
~8-minute cycle. A synthetic fixture would measure a distribution the desk does
not have, so an empty corpus aborts rather than inventing one.

Four properties it will not give up, each one a mistake that has already been
made here:

- **`--runs` defaults to 10, not 3.** At n=3 a 2/3 result is compatible with a
  true success rate anywhere from ~15% to ~95%, so "2/3 vs 3/3" is one unlucky
  run rather than a finding.
- **Arms interleave within each round.** All-A-then-all-B lets a busy box or a
  warming prefix cache pick the winner.
- **The cold start is reported separately, never in the median.** An idle
  Jetson pays ~21s on its first call.
- **Classification is fail-closed, and a known-bad arm is kept.**
  `agent-nominp` reproduces the pre-fix failure on demand (measured:
  `EMPTY_RESPONSE`, 1,539ms, 0 chars) — without it, a future "it works now"
  would be unfalsifiable.

### First full run — n=10 x 4 arms, interleaved, 2026-08-06

Corpus: 19 replayed `v3_regime_engine` prompts (the only shadowed role).

| arm | non-empty | valid artifact | median (warm) | median TTFT |
|---|---|---|---|---|
| `chat` | 10/10 | **10/10** | **16.2s** | **2.9s** |
| `agent` | 10/10 | 8/10 | 68.1s | 8.4s |
| `agent+tools` | 9/10 | 8/10 | 45.4s | 8.8s |
| `agent-nominp` (known-bad) | **0/10** | 0/10 | 1.4s | — |

**The `minP` failure is deterministic, not intermittent: 0/10.** Every call,
same ~1.4s, same zero bytes. The fix in `04-incidents.md` shipped on 0/3; at
n=10 there is no ambiguity left about the cause.

**For a role that does not need tools, `/chat` wins on both axes** — 10/10 vs
8/10 valid, ~4x faster, ~3x better TTFT. The tool catalog costs roughly 5.5s
before the first token even on the arm that never calls a tool. Both `/agent`
arms lost their two runs to the model wandering into prose instead of emitting
the artifact (one produced 28,955 characters).

**Scope, deliberately stated:** this measures `v3_regime_engine`, whose tools
show zero calls in 60 days. It is evidence about a *tool-less* job and says
nothing about a genuinely tool-using role. The operating rule it supports is
"`/chat` for pure processing, `/agent` when the agent must actually call
something" — not a blanket preference in either direction. Widen
`MODEL_SHADOW_AGENTS` to get a corpus that can speak for a tool-using role.

**`--phase concurrency`** is gated on `pipeline_state` being idle and fails
closed if that read fails: stressing a shared box during a live cycle degrades
the desk the benchmark is meant to measure. vLLM does not OOM under load — it
queues and preempts — so the knee to watch is `num_requests_waiting` lifting
off zero, not a crash.

Deliberately **not** wired into CI or `verify_deploy.py`: it is a
non-deterministic, minutes-long test against shared hardware, and gating a
deploy on the Jetson's mood is a coin flip, not a test.

## Shipped 2026-08-06 — `min_p=0` on the local vLLM boxes

Every prism `/agent` call from this service carried prism's injected
`minP: 0.05`, and a vLLM box running speculative decoding refuses it — after
answering `HTTP 200`, from inside the stream. The caller sees an empty
response, so it read as a model or prompt defect. Full diagnosis in
`04-incidents.md`.

Measured on the Jetson, interleaved, same prompt, one variable changed:
**0/3 non-empty → 3/3 non-empty and 3/3 a valid artifact.**

- `lazycat-sdk 0.3.10` (`327a73d`) — `AgentHarness.run` now forwards
  `BaseAgent.min_p`. It always existed on `call_agent`; the harness dropped it,
  so no caller could reach it. Unset stays `None`, so cloud providers keep the
  gateway default.
- `base_agent.min_p_for(provider, model)` — sends `0.0` for `vllm*` providers,
  `None` for cloud models (matched on the model name, because prism routes on
  the name and `provider` defaults to `"vllm"` even for an overridden model).
  Unknown providers get `None`, so a new endpoint cannot silently inherit it.
- A partial deploy (`app/` updated, `lazycat-sdk` not synced) would `TypeError`
  on every agent construction, so the SDK's signature is probed once at import
  and the fix degrades to a warning rather than an outage.

This is the root cause behind `GATEKEEPER_DEGRADED`; it is not a Gatekeeper
fix. Any agent on a local box was losing the same way, and the Gatekeeper was
merely the one pinned there.

## Re-verification against the database — 2026-08-06

Every claim in the two sections below was re-run against `shared_desk`,
`v3_agent_telemetry`, `decision_outcomes`, `trade_fills`, `v3_guardrail_firings`
and `trade_results`. Most held. Two things changed, and one prompted a
code fix.

**The debate figure below is superseded.** `38% (n=26)` was correct for the
three cycles it was computed over, and is reproducible from the database today.
A fourth post-fix cycle has since landed and it was the most bearish of the
four, so the pooled figure has moved:

| | bull | bear | tie | n | bear% |
|---|---|---|---|---|---|
| `1785978092` | 4 | 3 | 2 | 9 | 33% |
| `1785985682` | 1 | 4 | 2 | 7 | 57% |
| `1785991713` | 6 | 3 | 1 | 10 | 30% |
| `1786023000` | 2 | 6 | 2 | 10 | **60%** |
| **pooled** | **13** | **16** | **7** | **36** | **44%** |

Against a measured pre-fix baseline of **251 of 308 (81.5%)**:
P(≤16 of 36 | rate still 81.5%) = 7.4e-07; against the conservative 72%,
4.7e-04. **The asymmetry is still gone** — that conclusion survives the extra
cycle — but quote **44% over 36 verdicts**, not 38% over 26, and note that the
cycle-to-cycle swing is **30–60%**, wider than the three-cycle window showed.

Denominator, stated once because it is easy to get wrong: verdicts are
`shared_desk.debate_judge.winner` where the key is non-null. Desks where the
judge never produced a verdict are excluded (8 across the five cycles: 7 are
JSON `null` on aborted tickers, 1 is a `no_trade_available_gate` skip). Pooling
the **pre-fix** cycle `1785962005` (2 bull / 6 bear) into this table produces
`49%` and the false conclusion that the rework did nothing — that cycle ran
before the fix and belongs to the baseline.

**A `SELL` on a broken thesis is still unobserved, and now we know why.**
All 16 post-fix bear wins resolved to `HOLD`, none to `SELL`. That is correct
in every case: not one was a held name. Held names are `ALLY JPM LLY LMT VZ
NVDA EXLS TSM C HOOD COF`; the bear won on `AMD CARS SE CRH META TRMB DKS AMZN
CSCO HCA SHOP LI THC AG UBER PLTR`, and with no shorting a bear win on a name
the book does not own has no executable form. Only two held names reached a
debate at all (VZ, EXLS) and the bull won both. So the exit path is not
suspected-broken — it is **unexercised**, and the sample that would exercise it
is "held name × bear win", which has occurred **zero** times. Track that pair
rather than the SELL count.

Confirmed unchanged: defense-turn coverage (10/10 on each of the last two
cycles), the AGENT_ERROR trend (17.6% → 2.5%), the EXLS fill
(80.73 @ $33.70), VZ framed `POSITION_REVIEW` and kept, and the floor binding
on UNH/BLK/FCF/EPD. One fill went unmentioned earlier: `C`, BUY 10.84 @ $137.66
in `1785978092`.

**What the re-verification broke open:** the label that distinguishes a blocked
trade from a kept one was leaking, and two tests were red. Both are written up
in `03-open-items.md` under *"A blocked trade was still scoreable as a kept
one"* and *"The retry contract held in one branch and not its neighbour"*.

## Every fix live — `cycle-v3-1785991713`, and the debate result settles

The first cycle with the whole set deployed: per-desk framing, the restored
defense turn at its corrected budget, the exit frame, the repair fix, and the
corrected canary.

**The defense turn ran on 10 of 10 desks.** It was 8 of 11 with a 1-turn budget
and 6 of 8 after the budget fix; at full coverage the debate now has three
turns on every desk that holds one.

**Bear win rate, pooled over the three post-fix cycles:**

> **Superseded 2026-08-06 — quote 44% (n=36), not 38% (n=26).** The three rows
> below still reproduce exactly from the database; a fourth post-fix cycle has
> since landed at 60% bear and moved the pooled figure. See *"Re-verification
> against the database"* at the top of this chapter. The conclusion — the
> asymmetry is gone — is unchanged and now rests on a larger sample.

| | bull | bear | tie | bear% |
|---|---|---|---|---|
| cycle 1 | 4 | 3 | 2 | 33% |
| cycle 2 | 1 | 4 | 2 | 57% |
| cycle 3 | 6 | 3 | 1 | 30% |
| **pooled** | **11** | **10** | **5** | **38%** (n=26) |

Against the 72–94% measured over 288 debates beforehand:
P(≤10 bear wins of 26 | rate still 72%) = 3.6e-04. The asymmetry is gone, and
at n=26 that is no longer a small-sample story. Individual cycles still swing
(30% to 57%), so quote the pooled figure, not a cycle.

**The exit frame is working on real holdings.** Two held names came through:

```
VZ    [HELD]  POSITION_REVIEW  -> bull wins -> HOLD 65        (keep)
EXLS  [HELD]  DATA_SUFFICIENCY -> bull wins -> BUY 72 EXECUTED (add)
```

`EXLS` is the first execution on a name the book already owns — the "BUY =
add to the existing position" branch of the exit frame, taken deliberately
rather than as a fresh entry. **Still no SELL.** Both reviews concluded keep or
add, which are legitimate outcomes; the open question is whether a held name
whose thesis has actually broken now exits. That needs a broken thesis to
appear, so it cannot be forced.

**The floor bound again:** `FCF BUY @ 64 -> HOLD_POLICY_BLOCKED_LOW_CONFIDENCE`,
the second instance after `UNH`. Both mechanisms remain live — the Board
choosing HOLD, and the floor blocking a BUY it wanted.

## The artifact repair now recovers the agent's work — first measurement

`83cb633` gives the tool-less repair pass the agent's own tool results instead
of only its last sentence. It is firing as intended — recoveries of 3 to 12
tool results, 245 to 9,588 characters of findings, across judges, analysts,
the Board, the synthesizer and the defense turn.

| | repairs attempted | recovered | rate |
|---|---|---|---|
| Before (10h) | 100 | 49 | **49%** |
| After | 32 | 24 | **75%** |

**This one holds up.** At the first reading (12 of 18, 66%) it did not — that
could have come up by luck about 12% of the time, and it was recorded as
unproven. The sample nearly doubled and the effect strengthened rather than
regressed to the baseline: **P(≥24 of 32 | the rate were still 49%) = 0.0025**.
The 95% interval on the after-rate is 58–87%, so "about three in four" is the
honest description; 75% is the point estimate, not a precision claim.

Roughly half the repairs that used to be thrown away now come back as
artifacts. What it recovers is analysis the cycle already paid for — the agent
had done the research and merely narrated instead of emitting it.

**Measure it from `cycle_audit_log`, not the container log.** A deploy restarts
the container and `docker logs` starts empty, which silently made the
before-period read as zero. The audit log survives the restart; it is the only
source that can compare across a deploy.

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
