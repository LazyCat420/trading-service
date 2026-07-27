# HANDOFF — The skill loop now scores decisions instead of prose (2026-07-27)

Shipped `68a65a9`, **deployed** to `synology` 2026-07-27 20:22Z, container
healthy. This deploy also shipped the two waves that were sitting committed but
undeployed — the data-collection audit (`a4c763e` · `2bceb70` · `e5ad40b`, plus
lazycat-sdk `a201770`) and the CORAL repair loop (`cd7f606` · `51036c8`).

Previous handoffs archived:
[`docs/HANDOFF_data_collection_audit_2026-07-27.md`](docs/HANDOFF_data_collection_audit_2026-07-27.md) ·
[`docs/HANDOFF_coral_repair_loop_2026-07-27.md`](docs/HANDOFF_coral_repair_loop_2026-07-27.md).

**⚠ The data-collection wave's post-deploy checklist is now live and unrun** —
see [Verify next cycle](#verify-next-cycle).

---

## What this wave was

Autoresearch is not broken the way the evolution council was. It runs, outcomes
close cleanly (zero decisions unresolved past the 7-day window), and directives,
skills and `tool_playbook` all reach live agents. One link was broken: **the
thing deciding whether a skill edit survived was measuring prose.**

`_simulate_score_with_skill` returns `baseline + delta`, and the gate compared
`simulated - baseline` — so the realized-outcome term **cancelled exactly**.
Every accept and reject came from "contains a digit"-class heuristics. 66 of 70
recorded rejections scored the identical delta `-0.0050`.

Meanwhile `decision_outcomes.skill_versions` had been stamping every agent's
active version onto every decision since 07-25, and **nothing read it**.

---

## Every threshold here is measured, not chosen

This is the part worth keeping. Three numbers changed the design.

**1. n=25 was inside its own noise.** Bootstrapping 1500 real resolved decisions,
20k resamples:

| samples | 95% noise band between two IDENTICAL versions |
|---|---|
| n=25 (the old `MIN_DECISIONS_BEFORE_REEDIT`) | **±0.207** |
| n=50 | ±0.148 |
| n=100 | **±0.104** |

The 07-25 audit was right that churn made the loop unfalsifiable, and wrong
about the dose. Maturity is now **n=100**, margin **±0.104**. A version lives
~2-3 weeks. That is the price of a falsifiable loop.

**2. The obvious second tier does not exist.** Eval scores as a fast *quality*
signal fails on contact: excluding infra, agent-attributable eval failures run
at 0.5-1.3% — about **13 events in three weeks fleet-wide**. It cannot rank
anything. But the same data separates cleanly on a different question — share of
runs that never completed: median 1-4%, worst normal day 18%, and **100%** the
day DuckDuckGo began refusing our egress. So it became an **admissibility
filter**, not a score.

**3. 83% of "agent failures" were infra.** `classify_failure` sent anything whose
tool result contained "error" to `bad_arguments`. Sampled over 21 days, 86 of
~103 were transport — 40 × "Failed to reach trading-service", 36 ×
`lazy_web_search` "Search failed". Only ~17 were the agent's doing. That is why
the fundamental analyst's day-to-day failure band was ±0.53: it was tracking
provider uptime, not skill.

---

## What is live

- **`app/autoresearch/scorecard.py`** — `build_scorecard(agent, version)` and
  `regression_verdict(...)`. Verdicts: `UNCOVERED`, `IMMATURE`, `CONTAMINATED`,
  `HEALTHY`, `REGRESSED`.
- **`skill_optimizer` gate swapped.** Before any proposal is paid for, the
  serving version is scored against its predecessor. `REGRESSED` → revert.
  `CONTAMINATED` → hold. The prose heuristics survive, correctly labelled, as a
  pre-filter on obvious junk (`prose_prefilter` in `rejected_skill_edits`).
- **Rollback appends** the predecessor as a *new* version. Reactivating the old
  row would stamp two disjoint periods with the same number and every scorecard
  query would silently pool them. The reverted edit is dead-ended.
- **HOLD is scored** — as its own component, never folded into win rate. It is
  45% of decisions and was excluded from the old baseline entirely.
- **`scripts/skill_scorecard.py`** — `[--agent X] [--history] [--json]`.

```
maturity bar: n=100 resolved decisions   regression margin: ±0.104 (95% noise band)

   agent                      ver      n   score     dir    hold  incompl  verdict
.. v3_junior_analyst           24      0       —       —       —       7%  IMMATURE
.. v3_fundamental_analyst      19      0       —       —       —       1%  IMMATURE
```

---

## Verify next cycle

**From this wave** — nothing can be confirmed live yet, and that is expected:
version stamping began 07-25 and needs the 7-day resolve lag, so the first
non-zero `n` lands **~2026-08-01**. Until then every agent reads `IMMATURE 0/100`,
which is the correct answer.

1. `python scripts/skill_scorecard.py` — `n` should leave 0 in early August.
2. `failure_buckets` — new rows should start carrying `tool_unavailable` /
   `error_class='infra'`. If *every* new row is infra, the agent markers are too
   narrow; if none are, too wide.
3. SkillOpt log lines should read `held: … resolved decisions` rather than
   `rejected by score gate`.

**Carried over from the data-collection wave — now deployed and still unrun.**
Run one cycle on SBUX STT AGNC ASC BOOT NDAQ AMZN and check:

1. `agent_tool_telemetry` — `lazy_web_search` failures drop from 8/8; failure
   rows carry non-zero `elapsed_ms`.
2. `news_articles` — no new `FCF`/`RH`/`BLSH` rows.
3. `pipeline_events` — `_late` warnings where `_ok` used to appear.
4. `data_source_status.last_success` moves off 2026-06-24.
5. `price_history` — non-S&P tickers reaching the latest session, `source='polygon'`.

---

## Gotchas

- **`IMMATURE` and `UNCOVERED` are answers, not errors.** The previous gate
  always had an opinion and none of them were grounded. A loop that says "I
  cannot judge this yet" is the improvement.
- **`CONTAMINATED` is checked before maturity, deliberately.** A window whose
  tools were broken is inadmissible however many decisions it governed; ordering
  it after maturity would let it mature into a verdict it must never reach.
- **"Cannot tell" stays uncounted.** `classify_tool_failure` returns `None` for
  unrecognised failures rather than guessing. Call it infra and a bad skill hides
  behind it; call it agent and an outage reverts a good one.
- **Historical `failure_buckets` rows keep their original (wrong) labels.**
  Rewriting an audit log to match new code makes past measurements
  unreproducible. Anything before 2026-07-27 misattributes infra as
  `bad_arguments` — do not trust it for attribution.
- **Nothing consumes `error_class` yet.** When something does, it must gate on
  `ERROR_CLASS_ENGINEERING` and exclude `ERROR_CLASS_INFRA`, or a dead provider
  will queue CORAL repair jobs.
- **n=100 detects gross regressions only.** ±0.104 on a ~1% effect is not
  statistical power. This is a regression detector, not an optimizer, and the
  code says so.
- **`_SUPERSEDED_MIN_DECISIONS_BEFORE_REEDIT`** is retained only to carry the
  reasoning. Nothing reads it.

---

## Still open

**From this wave**
- Only 3 of 7 agents (fundamental, junior, quant) generate enough traces for the
  contamination filter; board/bull/bear/regime rely on the lagging tier alone.
- `eval_scores` and `failure_buckets` remain a closed loop otherwise — read only
  by autoresearch. The contamination filter is the first outside consumer.
- `cycle_directives` live ~3.8h before expiring (185 of 189 expired, 4 active).
  Not investigated — check whether any expire before a cycle reads them.
- The Auto-Research panel still reads `pending_evolution_fixes`, a graveyard: 88
  of 96 rows are May fossils written by code no longer in the repo.

**Carried over, still unaddressed**
- **`cycle_audit_log` has written nothing since 2026-07-25.** Not diagnosed.
- `put_call_ratio` is SPY-only, 6 rows ever; 07-25 duplicates 07-24 to 16 decimal
  places — a weekend stale-fill stored as an observation.
- `insider_trades` covers 14 micro-caps only.
- `social_posts` engagement counts are all null.
- `market_snapshots` appears abandoned — 819 rows, none written this cycle.
- The `congress_trades` future-dated row (2026-12-26) is guarded at read, not
  deleted.
- **Two pre-existing test failures** — `test_parameter_tools` and
  `test_tool_whitelists` — predate all of this. The CORAL loop produced a green
  branch for them (`evo/fundamental_analyst-80e6d46c`, local, unpushed) that
  reverts a *deliberate* 07-25 removal; it needs a human call, not a merge.

1576 unit tests pass (the 2 above unchanged).
