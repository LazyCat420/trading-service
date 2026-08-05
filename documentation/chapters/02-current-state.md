# Current state

Verified **2026-08-05** against `master@8182868` deployed to the NAS.

<div class="status-grid">
  <div class="tile ok"><div class="label">Tickers into analysis</div><div class="value">13</div><div class="note">was 0–1</div></div>
  <div class="tile ok"><div class="label">tool_playbook rows</div><div class="value">63</div><div class="note">purged from 4,948</div></div>
  <div class="tile ok"><div class="label">Playbook injection</div><div class="value">231 ch</div><div class="note">was ~120,000</div></div>
  <div class="tile warn"><div class="label">Agent tool-turn timeouts</div><div class="value">12</div><div class="note">new concern, see open items</div></div>
</div>

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
