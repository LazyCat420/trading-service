# Current state

Verified **2026-08-05** against `master@8182868` deployed to the NAS.

<div class="status-grid">
  <div class="tile ok"><div class="label">Tickers into analysis</div><div class="value">13</div><div class="note">was 0–1</div></div>
  <div class="tile ok"><div class="label">tool_playbook rows</div><div class="value">63</div><div class="note">purged from 4,948</div></div>
  <div class="tile ok"><div class="label">Playbook injection</div><div class="value">231 ch</div><div class="note">was ~120,000</div></div>
  <div class="tile warn"><div class="label">Agent tool-turn timeouts</div><div class="value">7</div><div class="note">new concern, see open items</div></div>
</div>

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

**Agents produce real artifacts again.** Cycle `cycle-v3-1785953340` had one
`no parseable artifact` occurrence across the whole run, against a **100%**
failure rate before the fix — every ticker in every discovery cycle since
2026-08-04 21:44 had aborted at the first agent. Sample output from
`v3_fundamental_analyst` on NYT, showing the pipeline doing genuine work:

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

After 80 minutes: **203 pipeline events, 6 tickers touched, 16 agents
completed**. The comparison that matters is the broken cycle from the same
morning — `cycle-v3-1785936600` produced **17 events total** and ended at the
gatekeeper.

## Not yet verified

**A full cycle has not been observed end to end since the fix.** The
verification cycle was still running at the time of writing, with no
`Saved analysis result` lines yet — those land when a ticker's whole pipeline
finishes. Selection, artifact generation, and per-agent completion are
confirmed. **Debate, trade execution, and cycle completion are not.**

**The cycle is slow.** 16 agents in 80 minutes, with individual agents taking
203–529 seconds. At that rate 13 tickers is a multi-hour run. Whether this is a
regression or was simply invisible while every agent failed instantly is
unknown — see [Open items](#open-items). It needs a baseline before anyone
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
