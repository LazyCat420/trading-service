# Session report — 2026-07-25

**Repos:** `trading-service` (`550a8e6` → `37a6d0a`, 14 commits) · `trading-client` (`060a6e2` → `33b2b8c`, 1 commit)
**Deployed:** backend `dc7cdb1`, client `33b2b8c` — both healthy on synology
**Tests:** trading-service **1295 pass**, 7 skipped (from 1241 at session start)

---

## What this session was

It started as "test the trading cycle and check the agent changes work." The
verification passed, and then a question about *why* the passing numbers looked
odd ("why are all 10 cases HOLDs?") uncovered a bug class that took the rest of
the session to close.

**Everything below is deployed and verified against live cycles.** Four cycles
were run through the deployed container; the last two passed **15/15**.

---

## 1. Verification of the prior wave (phases 0–6)

Ran a third cycle on tickers untouched by the original audit —
`cycle-observe-1784946884` (AXP/LLY/AMD), mixing held and unheld. **10/11
passed**; the 11th was a real gap, described in §2.

Three claims in the prior report were wrong and are corrected in the docs:

| Claim | Reality |
|---|---|
| "HRP mostly returns 0.0 — is the quant not populating it?" | **Neither.** HRP was never *running* (the `bot_id` bug). 8 desks now carry real varying weights. |
| "The synthesizer never overrides the board — 0 of 53" | **Retracted.** At n=557 it differs on **108 (19%)**, incl. 8 hard BUY↔SELL flips. |
| "`technicals` is stale — GOOGL 7d, IP 9d" | Far worse: **5 of 503 tickers (1%)** fresher than 3 days. Root-caused in §3. |

I also **corrected my own error**: I first reported that AMD proved the
no-shorting fix "works by persuasion — the coercion never fired." It *did*
fire, on the synthesizer. Coercions are recorded in **artifact metadata, not
logs**, and I had checked only the logs.

---

## 2. Decision integrity — the main body of work

### The question that started it

~2% of desks had no `final_decision` despite a decision being made, and all the
observed cases were HOLD. That looks like a decision bias. **It is not.**

- **HOLD's base rate is 52%** of the sample (not ~33%), so 9-in-a-row is
  ~1-in-344 — unlikely, not astronomical.
- **9 of 10 desks had `trade_decision` fully persisted** at `PM_DONE`. The
  pipeline produced and saved a real verdict; only the board's artifact was
  missing.
- **One cause, both effects:** `final_decision` only propagated on a
  SUCCESS/DATA_GAP board outcome (`orchestrator.py:1442`), and a degrading
  board *also* fell back to a hardcoded `{"action":"HOLD","confidence":0}`
  (`orchestrator.py:798`).

The real defect was narrower but serious: **a degraded board was
indistinguishable from a confident no-signal HOLD**, so every accuracy number
silently counted degraded HOLDs as real opinions.

### The governing principle

> **A degraded result must never be representable as a confident one.**
> This codebase's failure mode is *laundering*, not crashing.

### What shipped (`eac617a`, `d42785a`, `3f5db39`, client `33b2b8c`)

- **`DecisionProvenance` stamped inside `append_artifact`**, not at the ~6 call
  sites — a new fallback path *cannot* emit an unmarked decision by forgetting.
  This was the **third** unmarked-fallback bug (timeout, degrade, `bot_id`), so
  it is now structural rather than conventional.
- **`final_decision` always writes.** A degrade records an explicit sentinel
  (`action: None` + `degrade_outcome`), never a fake HOLD. `null` used to mean
  both "never ran" and "ran and we lost it".
- **Guardrails are countable** — new `v3_guardrail_firings` table, and
  `coerce_unshortable_sell` now names the ticker. It previously logged neither,
  which is exactly how I misdiagnosed AMD.
- **`decision_provenance` reaches `trade_results`** (PG + Mongo mirror) and the
  API (`is_agent_decision`), and the **UI shows an amber "NOT AN AGENT
  DECISION" band**. Missing → **NULL**, never defaulted.
- **Scorecard excludes degraded rows by default** (`--include-degraded` to opt
  out) — every wrong headline in this audit came from a permissive default.
- **Standing reconciliation** of `shared_desk` ↔ `trade_results`, on both action
  **and** provenance.
- **10 historical desks backfilled** (`scripts/backfill_desk_decisions.py`)
  after a 1766-row backup, stamped `_backfilled_from`.

### Two mistakes I made and caught

1. **The provenance filter shipped broken** — it read `trade_decision` first,
   so on backfilled desks (whose `trade_decision` predates the field) a
   degraded `final_decision` was masked: it reported 0 degraded when there were
   7. A filter that fails open on exactly the rows it exists to catch is worse
   than none. Fixed in `d42785a`, pinned by a test.
2. **The field only reached half its consumers.** It passed a 14/14 run while
   `trade_results` — what the UI, replay API and freshness gate read — was
   still blind to it. The reconciliation check missed it because it compared
   only the **action**. Two stores agreeing on "HOLD" while disagreeing on
   whether anything *decided* it is the same laundering. Fixed in `3f5db39`.

---

## 3. `technicals` staleness — never a collector gap

`price_history` was current for every ticker the whole time. **Three bugs in
the derived-indicator writer** (`e04c7b9`, `89174d6`, `dc7cdb1`):

1. **It read the OLDEST prices.** `ORDER BY date ASC LIMIT 500` — MSFT (10,169
   rows back to 1986) recomputed `1986-03-13..1988-03-03` every run. **CVX's
   newest technical row was 1963-12-26** against a 2026-07-24 price: a
   **22,856-day lag**, served to the quant analyst under the header *"these are
   the authoritative values"*.
2. **`ON CONFLICT DO NOTHING`** meant a re-run could never *correct* a row,
   only add missing ones → damage only accumulated. Now `DO UPDATE`.
3. **Nothing scheduled it.** It ran only when an agent happened to call
   `get_technical_indicators`, hence the ragged 8–71 day staleness.

**Result: 2708 tickers, 100% fresh** against their own price history (was 5 of
503). `compute_technical_baseline` now returns `stale=False age=0d`, so the
Phase 4 quant reconciliation is authoritative for the first time.

**A third mistake:** I first hooked the refresh into `collect_all()`, which the
V3 precollect path never calls — it invokes `collect_price_history` directly,
so it would have fired only on the scheduler path and every *cycle* would have
stayed stale. Caught by tracing a live cycle rather than trusting my passing
test.

---

## 4. Live verification

| Cycle | Tickers | Result |
|---|---|---|
| `...1784946884` | AXP/LLY/AMD | 10/11 (the 11th exposed §2) |
| `...1784949769` | AXP/INTC | 14/14 |
| `...1784951526` | MSFT/KO | **15/15** — provenance chain end-to-end |
| `...1784955045` | UNH/CVX | **15/15** — fresh technicals end-to-end |

Two results worth keeping:

- **The units fix is visible in the board's own reasoning.** AXP's `SELL @75`
  cites *"HRP target weight (3.3% equity) is significantly lower than current
  exposure (8.3% equity)"* — the corrected conversion being reasoned over.
- **Both desks' RSI matches `technicals` exactly**, `as_of=2026-07-24`: CVX
  **71.44** (was a *1963* value), UNH **51.56**.

---

## 5. Traps worth keeping (all in HANDOFF)

- **Guardrail firings are in artifact metadata, NOT logs.** Check
  `_coerced_from` / `_validator_notes` before concluding one didn't run.
- **HRP weights are of INVESTED capital, not equity** — ~2× apart on a
  47%-cash book. That is what produced the 19.2% VZ order; the correct figure
  was 7.9%. The bracket now states the basis and calls HRP a *ceiling*.
- **The technicals write must stay batched.** A loop of `execute()` cost
  **22.6s/ticker** (~16h full repair); `executemany` → ~0.1s warm, 2631 tickers
  in **321s**.
- **The indicator floor is 28 sessions, not 14.** ADX smooths an
  already-smoothed series; `ta` *raises* on short frames rather than returning
  NaN.
- **The postgres container has the Docker-default 64MB `/dev/shm`.** Grouped
  JOINs over `price_history` fan out to parallel workers and fail. Not our
  container — shape the query around it.
- **Desks are written incrementally.** A mid-flight read shows
  `phase=DEBATE_DONE` with null decisions, indistinguishable from the bug fixed
  here. Check `phase=PM_DONE` **and** `updated_at`.
- **Don't deploy while a cycle is in flight** — the restart kills it. I did.

---

## 6. Open / not done

- **Nothing from the original backlog is open.** The `technicals` collector gap
  was the last item and is closed.
- **The degraded-sentinel path has only ever been unit-tested.** Every live
  cycle has been healthy, so `board_degraded_fallback` has never been exercised
  end-to-end. Worth forcing once (with `trade=False`) to prove the UI band and
  the scorecard exclusion behave on real data.
- **`v3_guardrail_firings` is empty.** Correct — coercion has fired once in 852
  desks — but it means the table itself is unproven in production.
- **Only 5 `trade_results` rows carry provenance** (everything before today is
  NULL = unknown, deliberately not backfilled as `board_reasoned`). Accuracy
  filtering gets more meaningful as rows accumulate.
- **Idea not built:** a *distribution-collapse canary*. Every finding this
  session was a distribution, not an error — 10/10 one value, 79% of sizes in
  {3,4,5}%, `trend_strength` averaging 0.81 with zero tool calls, 56%
  fabricated RSIs. All found by hand, months apart. One weekly job flagging any
  agent field whose distinct-count collapses would have caught all of them.

### Not mine, but noticed

- **`trading-client` has 5 pre-existing test failures** in the cycle-status /
  pipeline-router area. Confirmed present on the commit *before* my change, so
  unrelated to this work — but they are red.
- **I consumed an old `trading-client` stash by mistake.** A `git stash pop`
  popped a stale entry (from before the `trading-backend → trading-service`
  rename) rather than my own no-op stash. The working tree was restored to
  `33b2b8c` with no loss to current work, and 2 stashes remain, but that old
  entry is gone.
