# HANDOFF — invariants, hooks, cost accounting (2026-07-30)

Continues `HANDOFF_harness_hooks_2026-07-30.md`. Of its five open items:
**1 and 2 done, 3 done in substance, 4 belongs to a concurrent session, 5 is
cross-repo.**

**Deployed `7ec0913` at 08:30 UTC**, container healthy, every change verified
running inside it. Suite: **2,244 passed, 20 skipped, 1 failed** — the failure is
`test_whitelists_grant_write_to_pm_and_board_only`, long-standing and
reproducible on clean master. Treat as green.

---

## Read this first

Two of my own claims in this session were **wrong**, and both failed the same
way: I trusted a verification that could not fail. The findings survived; my
confidence in them did not. "What I got wrong" is the most useful section here.

---

## What shipped

| change | where | status |
|---|---|---|
| `DESK_STALLED_MID_PIPELINE` — 6th cycle invariant | `app/v3/invariants.py` | **fired on a real regression** |
| `flush_agent_telemetry` on every `save_desk` | `app/v3/telemetry.py`, `desk_persistence.py` | **verified on live rows** |
| `record_ticker_crash` — persists WHY a ticker died | `app/v3/invariants.py`, `pipeline_service.py` | live, never triggered |
| Pre-flight tool-argument repair | `app/v3/tool_repair.py` + `routers/agent_tools_router.py` | on the right path, **never fired** |
| `live_db` fixture — makes live tests able to fail | `tests/conftest.py` | fixed 3 hollow tests |

### The invariant reads `shared_desk`, deliberately

Every pre-existing cycle check keyed off `analysis_results` — **the table whose
absence is the symptom.** A desk stranded mid-pipeline was invisible to all seven
of them. This one reads `shared_desk`, the one table a stalled desk is guaranteed
to appear in, because that row is written on the way *in*.

Terminal phases are **derived** from `_VALID_TRANSITIONS`, not duplicated, so a
new `DeskPhase` cannot leave it asserting a stale vocabulary. `INIT` is excluded:
it is a legitimate Triage-Gate skip at ~22/week, and firing on those would have
made the check ~79% false positives in week one.

It reports **two losses separately** — `lost_research` (nothing persisted) and
`undecided` (research persisted, no decision). See "What I got wrong #2".

---

## Verified vs unverified — the distinction matters

**Verified on live production traffic:**

- **The stall detector caught a real regression within minutes of it shipping.**
  NVDA, `cycle-observe-1785396275`, 07:28. Root cause `6a9bd82`: the
  `DEBATE_ENGINE=3` branch returned early and skipped the `tournament_result`
  whiteboard write — **that write is the chain trigger that dispatches the
  Board.** Seven agents ran, the Board never did, no decision was produced.
- **The cost flush works.** Cleanest possible before/after on live rows:

      after   NVDA  RESEARCH_DONE   5 telemetry rows   <- mid-flight
      after   JPM   DEBATE_DONE     9 telemetry rows
      before  HOOD/CARS/EXLS/OWL/UNH, same phases      0 rows each

  Rows existing while a desk is still non-terminal is the proof — the old single
  end-of-pipeline write could not produce that.

**NOT verified, and one is a live question:**

- **`record_ticker_crash` has never fired.** No ticker has raised since deploy.
  Untriggered, not proven.
- **The tool repair has never fired, and may now be moot.** In the 11 hours after
  deploy: 169 tool calls; `v3_fundamental_analyst` (the agent that was failing)
  ran 89 of them including 11 `get_sec_filings` calls — **0 rejections, 0
  repairs.**

      prior rejection rate     21/62 = 33.9%   (07-28..07-30, pre-deploy)
      post-deploy              0 / 11
      P(0 | old rate)          0.011

  A drop that large is unlikely by chance, **but zero repairs were recorded, so
  it is not attributable to the hook.** Something upstream stopped the malformed
  JSON — plausibly the same prompt/engine work that produced `6a9bd82`. **Do not
  record this as "the repair works."** It is correctly placed and unproven.

---

## What I got wrong

### 1. The repair hook shipped inert, and my test said it was fine

The module, allow-list and tests were all correct. It was wired only into the
**local** `AgentHarness.on_tool_call` in `base_agent.py` — and **that hook never
runs for a V3 pipeline agent.** V3 agents execute inside prism-service, so their
tool calls arrive back over HTTP at `POST /agent-tools/execute`
(`app/routers/agent_tools_router.py`) and go straight to
`registry.execute_tool_call`. **trading-service has two tool dispatch paths, and
the repair sat on the unused one.**

Measured: 16 `get_sec_filings` rejections, every one from
`v3_fundamental_analyst` with the ticker already known, **zero repairs recorded**
— two landing *after* the repair shipped.

My verification was the defect:

- the reachability test asserted `AgentHarness` receives an `on_tool_call` —
  true, and irrelevant;
- my in-container check called `make_pre_tool_hook(...)` **directly**, so it set
  its own context and proved the function works while proving nothing about
  whether anything calls it.

Fixed by a concurrent session in `184758a`: repair in the HTTP bridge before the
`tool_call` payload is built, reusing `repair_tool_arguments` with the
fail-closed allow-list intact.

> **Rule:** before wiring any tool-layer hook, ask which dispatch path the target
> agents use. Then verify with **telemetry, not reading** — a repair that fires
> leaves a row. Zero rows beside non-zero rejections means it is not on the path.

### 2. I called a real regression benign

I reported NVDA's stall as the "benign shape" because its analysis and 7
telemetry rows survived and it carried a `pipeline_incomplete` stamp. Both
inferences were wrong:

- a `pipeline_incomplete` stamp explains the **phase**, not the **outcome**. I
  read a diagnostic as evidence of health;
- one `lost` count flattened two different losses. A desk can lose its
  *research* (nothing persisted, spend gone) or its *decision* (the thing the
  pipeline exists to produce). Flattening them is what let a live regression read
  as healthy.

Now reports `lost_research` and `undecided` separately. Replayed over 49 live
cycles: the NVDA cycle reports `undecided=1`; the four historical stalls report
`lost_research=1/2/3`.

---

## Findings that should change decisions

**1. Up to ~14.5% of token spend was never recorded.** `persist_telemetry` ran
once, at the very end of `run_v3_pipeline`, so cost sat in memory and a ticker
that died first lost its whole record. Since 2026-07-12, when this telemetry
begins — *any* coverage figure spanning that boundary averages two populations
and reads as a partial outage:

    PM_DONE        429 desks   99.5% have cost rows
    ABORTED         40 desks    0.0%
    DEBATE_DONE      9 desks    0.0%
    RESEARCH_DONE   22 desks    4.5%

71 desks with no cost record at a median 664,627 tokens each is **~47M tokens**,
upper bound. **Consequence: any share-of-spend number taken from this table —
including the previous handoff's "the tournament is 31% of all tokens" — was
computed against a denominator missing the crashed tickers.**

**2. The `-k live` audit had never measured anything.** conftest's autouse
`patch_get_db` hands every test a MagicMock: `fetchall()` returns `[]`,
`fetchone()` returns `None`. `test_recent_completed_cycles_are_self_consistent`
skipped with *"no completed cycles on record"* against a database holding **675**
of them; two siblings raised `TypeError` on `None`. It surfaced only because I
had written `assert stalled, "this test proved nothing"`.

Fixed with a `live_db` fixture (real connection, `SET
default_transaction_read_only = on`). **Every live test now takes it and opens
with a vacuity guard.**

**3. Audited all 83 test files touching `get_db` — the trap was confined to that
one file.** A verified negative, so the suite can be trusted here. The two
modules overriding `patch_get_db` (`test_db_constraints`,
`test_connection_pool_exhaustion`) are deliberate and neither reaches production;
the ~20 `mock_db` overrides are the intended composition. If you re-run a similar
scan: a heuristic pass produced 19 other hits, **all false positives** — they
assert on captured writes (`cap["sql"]`) or patch `get_db` to raise deliberately,
to prove fail-open.

**4. Stalls are per-ticker, not per-cycle.** `gather(..., return_exceptions=True)`
(`pipeline_service.py:1628`) isolates a raising ticker — correct, and why
siblings finish normally. HOOD died at `DEBATE_DONE` while EXLS and CRH completed
12 minutes later in the same cycle.

**5. The repair must stay fail-closed.** 29 tools require a `ticker`, including
`buy_stock`, `sell_stock`, `add_to_watchlist`, `watch_ticker`. Completing a
malformed **order** with a guessed ticker is not a repair, it is an invented
trade. Allow-list only; `buy_stock` is asserted to stay rejected by the same
oracle that proves the others fixed.

---

## Open work, in priority order

### 1. Re-run the budget numbers now that cost accounting is complete

The flush is live and working. Every share-of-spend conclusion from before
2026-07-30 used a denominator missing up to 14.5%. Cheap, and it changes
decisions — the tournament's 31% share was the basis for retiring it.

### 2. Watch whether the repair ever fires

Correctly placed, never fired. Either the upstream fix made it moot (then it is
cheap insurance) or malformed calls return.

```sql
SELECT detail->>'tool', detail->>'agent', COUNT(*)
FROM v3_invariant_violations
WHERE kind = 'TOOL_ARGS_REPAIRED_PRE_FLIGHT' GROUP BY 1,2;

-- and the failures it should be preventing
SELECT created_at::date, COUNT(*) FROM agent_tool_telemetry
WHERE NOT success AND created_at > NOW() - INTERVAL '7 days'
  AND (error_message LIKE '%Required field%' OR error_message LIKE '%missing 1 required%')
GROUP BY 1 ORDER BY 1;
```

### 3. `PIPELINE_COMPLETE_BUT_NO_DECISION` fired twice today, last at 13:36

That is *after* the Board regression was fixed. Check whether it is the same
cause recurring or something new — the violation names the ticker and cycle.

```sql
SELECT kind, ticker, cycle_id, detail, created_at
FROM v3_invariant_violations ORDER BY created_at DESC LIMIT 20;
```

### 4. Recovery, not just observability

Nothing yet *recovers* a stalled desk. A ticker that dies after the debate is
paid for still produces no decision. Whether to retry or salvage into a
degraded-provenance HOLD is a product call; `BOARD_DEGRADED_FALLBACK` exists for
exactly this shape.

### 5. Do NOT wire `check_ticker_complete` into the crash path

I measured what it would emit; it is not the gap it looks like:

    desk exists, crashed  ->  PIPELINE_COMPLETE_BUT_NO_DECISION
    crashed before desk   ->  TICKER_ANALYSED_BUT_NO_DESK

The first is a row whose **name asserts something false**; the second is strictly
weaker than the `phase_at_crash` + exception type `record_ticker_crash` already
writes. Two observers are only worth having when they can disagree. The reasoning
lives in `record_ticker_crash`'s docstring so it does not get re-litigated.

### 6. Cross-repo: tool attribution (unchanged, not ours)

`McpAdapter.ts:70` and `ToolOrchestratorService.ts:1583` in **lazy-agent-service**.
`agent_tool_telemetry` already has full attribution — it is what let me measure
everything here — and the two tables are **not** redundant.

---

## Traps

- **Every pytest test gets a MagicMock database.** Take `live_db` and open with a
  vacuity guard. An audit that cannot fail is not an audit.
- **Two tool dispatch paths.** Local `AgentHarness` hook vs the HTTP bridge in
  `agent_tools_router.py`. V3 uses the bridge.
- **`on_tool_call` is called UNGUARDED by the SDK** (`lazycat/agent.py:320`),
  unlike `on_tool_result` which it wraps. A raise kills the agent's turn, and a
  non-None return **blocks the call**.
- **`tool_schemas.json` is a gitignored build artifact absent from worktrees.**
  Copying a **stale** one is worse than having none — it converts deliberate
  skips into false failures. Three phantom failures cost me time this way. Copy
  fresh from the main checkout, or leave it absent.
- **The registry stores schemas WRAPPED** (`{"type":"function","function":{…}}`)
  while the file on disk is **flat**. Hand-populating `registry.schemas` makes
  every lookup miss, drop nothing, and report ACCEPTED. My first oracle did this
  and "confirmed" a payload production had rejected. **Load via `load_from_json`.**
- **Truncating a payload while inspecting it can invent a bug.** My first
  whiteboard fixture omitted `section` because I printed `args[:120]`. The
  `TypeError` named exactly **one** missing argument — check the arity in the
  error against your fixture.
- **A repair makes its own defect invisible.** Once it injects the ticker the
  call succeeds and leaves the failure telemetry entirely. Recording the repair
  is the only thing keeping the upstream bad-JSON bug measurable.
- **Cycle cadence is irregular** — 1.5–6h gaps are normal. A quiet two hours is
  not a fault; check `shared_desk` before concluding the scheduler broke.
- **A concurrent session can deploy over you.** Verify what is RUNNING —
  `docker inspect --format '{{.State.StartedAt}}'` plus an `exec` import check —
  rather than assuming your deploy is live.

---

## Method notes worth keeping

Every real finding came from measuring rather than reading; both mistakes came
from trusting a verification that could not fail.

- **Verify a detector FIRES, not merely that it is silent.** Positive replay
  (3 of 48 cycles, 6/6 desks) plus negative controls (83 `INIT`-only and 34
  `ABORTED`-only cycles → 0 fires).
- **Mutation-test the guarantees.** 12 mutations across these changes; each fails
  a test. Two initially **escaped**, and the pattern is worth knowing: every test
  stubbed `_persist_entries`, so deleting its `raise` broke nothing; and a wiring
  test matching a *name* passed when the call was deleted but the import kept —
  it now asserts the call via AST.
- **Check for two populations before believing a coverage number.** "PM_DONE is
  64% covered" was really 0% before 2026-07-12 and 100% after.
- **Watch a new detector's FIRST live firing.** That is where the shape you did
  not calibrate on appears — it is how both the two-loss split and the Board
  regression surfaced.

---

## Cleanup

This worktree is fully merged but still checked out — it was the session cwd, so
it cannot remove itself. A second worktree, `debate-engine-off`, belongs to the
concurrent session; leave it alone unless that session is done.

    git worktree remove --force .claude/worktrees/fidelity-followup
    git branch -D worktree-fidelity-followup

`tool_schemas.json` was copied in so the allow-list guard tests could run. It is
gitignored, was not committed, and disappears with the worktree.
