# HANDOFF — hooks follow-up: items 1 and 2 of the previous plan (2026-07-30)

Continues `HANDOFF_harness_hooks_2026-07-30.md`. Two of its five open items are
done; the other three are re-ranked below with what I learned about them.

Merged, pushed and **DEPLOYED** at `9fb797d` (2026-07-30 05:48 UTC) — container
healthy, new code verified live inside it (allow-list 19 tools, `buy_stock`
correctly NOT repairable, a repair smoke test injected the ticker end-to-end,
`on_tool_call` wired). Deployed only after the running cycle drained.
**Remaining: confirm both features fire on real traffic — see the bottom.**

Suite: **2,186 passed, 20 skipped, 2 failed.** Both failures reproduce on clean
master (`test_whitelists_grant_write_to_pm_and_board_only` long-standing,
`test_prism_prompt_injection` needs VLLM 10.0.0.141:8000). Treat as green.

---

## The one-paragraph version

Item 1 closed the blind spot the last session created: desks abandoned
mid-pipeline were invisible because every cycle-level check keyed off
`analysis_results`, the table whose absence *is* the symptom. Item 2 wired the
first hook that acts *before* a tool call instead of recording its failure
afterwards. But the finding with the longest shelf life is neither of those:
**the `-k live` audit path had never measured anything.** conftest's autouse
`patch_get_db` hands every test a MagicMock, so the live checks the previous
session shipped were asserting against `[]` and `None`. That is the same defect
class both sessions have now been hunting — an observer keyed off something
that cannot report the failure — and it had quietly infected the instrument
used to validate the other observers.

---

## Findings that should change decisions

**1. The live audit was hollow, and only a vacuity guard caught it.**
`tests/conftest.py` patches `app.db.connection.get_db` for **every** test
(autouse). `fetchall()` returns `[]`, `fetchone()` returns `None`. So
`test_recent_completed_cycles_are_self_consistent` skipped with *"no completed
cycles on record"* against a database holding **675** of them, and the other two
live tests raised `TypeError` subscripting `None`. I only noticed because I had
written `assert stalled, "this test proved nothing"` and it fired. Fixed with a
`live_db` fixture that overrides the patch with a real read-only connection.
**Every live test now takes `live_db`, and every one has a guard that fails when
its window is empty.** An empty result is not evidence of health.

**2. Stalled desks are ~5% of all desks, not 3%, and they are per-ticker.**
Over 30 days: **55 of 1,048 desks (5.2%)** in **22 of 262 cycles**. The 7-day
figure from the last handoff (6 of 204) reproduced exactly. Worth noting: in
`cycle-v3-1785382116`, HOOD stalled at `DEBATE_DONE` while EXLS and CRH in the
**same cycle** reached `PM_DONE` twelve minutes later. So this is **not** a
deploy killing a whole cycle — it is one desk dying while its siblings finish.
That rules out the most convenient explanation, and it is the clue that led to
the mechanism: `gather(return_exceptions=True)` isolates a raising ticker. Traced
in full under open item 1.

**3. `get_sec_filings` is still failing, and the previous handoff's "fixed" row
is about a different bug.** That table credits a fix for *"142/510 calls (27%)
rejected pre-execution on a key-name mismatch"*. Measured now: **26.3% failure
over 14 days (n=57)**, with rejections on **07-28 (8), 07-29 (6), 07-30 (4)** —
ongoing, not decaying. The live failures are a *different* mechanism: the model
emits un-escaped JSON, the real keys never parse, and the required `ticker` is
lost with the wreckage. Both bugs are real; only one was fixed. **Do not read
that row as "SEC lookups are healthy now."**

**4. The repair had to be fail-closed, and that is not a stylistic choice.**
29 tools declare `ticker` as required. Four of them (`buy_stock`, `sell_stock`,
`add_to_watchlist`, `watch_ticker`) change money or persistent state, plus
`escalate_to_pm`, `request_peer_analysis`, `save_trading_chart`, `run_equation`,
`run_backtest`. "Inject the ticker wherever the schema wants one" would complete
a malformed **order** with a guessed ticker. That is not a recovered call, it is
an invented trade. Allow-list, asserted by test, with `buy_stock` proven to stay
rejected by the same oracle that proves the others fixed.

**5. Up to 14.5% of token spend was never recorded.** `persist_telemetry` ran
once, at the very end of `run_v3_pipeline`, so each agent's cost sat in memory
until then and a ticker that died first lost its whole cost record. Since
2026-07-12 (when this telemetry begins — *any* coverage figure spanning that
boundary averages two populations and reads as a partial outage):

    PM_DONE        429 desks   99.5% have cost rows
    ABORTED         40 desks    0.0%
    DEBATE_DONE      9 desks    0.0%
    RESEARCH_DONE   22 desks    4.5%

71 desks with no cost record at a median 664,627 tokens each is **~47M tokens
(~14.5%) invisible**, upper bound. Consequence worth carrying: any
share-of-spend number taken from this table — including the previous handoff's
"the tournament is 31% of all tokens" — was measured against a denominator
missing the crashed tickers.

**6. The pre-tool repair shipped onto the WRONG DISPATCH PATH and did nothing.**
The module, allow-list and tests were all correct; it was wired only into the
LOCAL `AgentHarness.on_tool_call` in `base_agent.py`, and **that hook never runs
for a V3 pipeline agent.** V3 agents execute inside prism-service, so their tool
calls arrive back over HTTP at `POST /agent-tools/execute`
(`app/routers/agent_tools_router.py`) and go straight to
`registry.execute_tool_call`. Two dispatch paths; the repair sat on the unused
one. Measured: **16 `get_sec_filings` rejections, every one from
`v3_fundamental_analyst` with the ticker already known, and ZERO repairs
recorded** — two of them landing *after* the repair shipped.

My verification was the problem, not the code. The reachability test asserted
`AgentHarness` receives an `on_tool_call`, which is true and irrelevant; and my
in-container check called `make_pre_tool_hook(...)` **directly**, so it set its
own context and proved the function works while proving nothing about whether
anything calls it. That is [[a-probe-that-sets-its-own-context-proves-nothing]],
which I already had written down. Fixed by a concurrent session in `184758a` —
repairing in the HTTP bridge before the `tool_call` payload is built, reusing
`repair_tool_arguments` with the fail-closed allow-list intact.

**Before wiring any tool-layer hook, ask which of the two dispatch paths the
target agents use, and verify with TELEMETRY: a repair that fires leaves a row.
Zero rows beside non-zero rejections means it is not on the path.**

**7. The stall detector called a real regression benign, because I flattened two
different losses into one count.** NVDA (`cycle-observe-1785396275`, 07:28) kept
its analysis and 7 telemetry rows, so my first refinement reported `lost=0`. The
cause was a same-day regression (`6a9bd82`): the DEBATE_ENGINE=3 branch skipped
the `tournament_result` write, **which is the chain trigger that dispatches the
Board**. Seven agents ran, the Board never did, no decision was produced.

Two mistakes of mine, both now fixed: a `pipeline_incomplete` stamp explains the
**phase**, not the **outcome** — I read it as "the ORCL fix working as designed"
and inferred health from a diagnostic; and a desk can lose its *research* or its
*decision*, which are not the same loss. Now reports `lost_research` and
`undecided` separately. On replay the NVDA cycle reports `undecided=1`.

**The detector did its job**: it flagged a regression within minutes of that
regression shipping. My classification of it was the weak link.

**8. The SDK already had the seam.** `AgentHarness` has accepted
`on_tool_call` all along; trading-service just never passed one. It also passes
the **same** `arguments` dict to `execute_tool` afterwards, so an in-place
mutation is what the tool receives. No SDK change — which matters, because
lazycat-sdk is shared with html-notes/canvas/music.

**Caveat, learned the hard way (finding 6): having the seam is not being on the
path.** That hook is real and works; V3 agents simply do not go through it.

---

## What shipped

| change | evidence |
|---|---|
| 6th cycle invariant `DESK_STALLED_MID_PIPELINE` | reads `shared_desk`, not `analysis_results`; fires on 3/48 cycles (6/6 desks), silent on 45 |
| `live_db` fixture + guards on every live test | the `-k live` audit went from 2 failed + 1 hollow skip to **16 passed** against production |
| `PreToolUse` hook (`app/v3/tool_repair.py`) | injects the missing required `ticker`; both production rejection paths confirmed cured against the SDK's own validator |
| Repairs recorded as `TOOL_ARGS_REPAIRED_PRE_FLIGHT` | a repaired call vanishes from failure telemetry; without this the upstream bad-JSON bug goes invisible |
| `record_ticker_crash` → `DESK_ABANDONED_MID_PIPELINE` | the crash was log-only; now the exception **type** is persisted (`asyncio.TimeoutError` stringifies to `""`) with the phase it died at |
| `flush_agent_telemetry` on every `save_desk` | agent cost was written once at the very end, so ABORTED/DEBATE_DONE desks had **0%** coverage vs 99.5% for PM_DONE — up to **~47M tokens, ~14.5% of true spend, invisible** |
| `DESK_STALLED` separates lost work from a stale phase | the first LIVE firing was the benign shape; collapsing them would have muted the check |
| Reachability guards on both hooks | nothing exercised the `AgentHarness` construction, so deleting `on_tool_call` would have left `tool_repair.py` as dead code reading as shipped |

Terminal phases are **derived** from `_VALID_TRANSITIONS`, not duplicated, so
adding a `DeskPhase` cannot leave the check asserting a stale vocabulary.

### How all three were validated

Not "the tests pass" — the previous handoff's own warning was that two of five
cycle checks passed a silence test and would have missed their motivating defect.

- **Positive replay**: the stall check fired on exactly the 3 of 48 cycles
  containing a stall, catching 6/6 desks.
- **Negative controls**: **83** INIT-only and **34** ABORTED-only cycles → 0
  fires. `INIT` is a legitimate triage skip; production ran 22 in a week, so
  firing on them would have made the check 79% false positives in week one and
  muted within days.
- **Mutation testing**: 12 mutations across all three changes (INIT no longer
  excluded, always-silent, unregistered, cap removed, `buy_stock` allow-listed,
  allow-list bypassed, overwrite-model-ticker, hook blocks, hook try/except
  removed, hook unwired, crash-recorder stamps ABORTED, error_type dropped, crash
  call site removed). **Every one fails a test.**
- **Oracle, not re-implementation**: the repair is validated by loading
  `tool_schemas.json` through the registry's own `load_from_json` and asking its
  own `_filter_kwargs_to_schema`/`_schema_params`. It reproduces the exact
  production error strings before, and accepts after.

---

## Open work, in priority order

### 1. Fix the stall: an exception escapes `run_v3_pipeline` and nothing stamps the desk

**Diagnosed, and half-fixed.** `record_ticker_crash` now persists the exception
type and the phase it died at (`DESK_ABANDONED_MID_PIPELINE`), so the next stall
arrives with its cause attached instead of requiring container logs. What remains
is acting on it — the pipeline still loses the desk's work.

The chain, all line numbers current as of `d294560`:

1. `pipeline_service.py:1628` — `asyncio.gather(*tasks, return_exceptions=True)`.
   Deliberate (one bad ticker must not kill the batch), and it is what makes the
   failure *per-ticker*: siblings complete normally. That explains HOOD stalling
   while EXLS and CRH finished 12 minutes later in the same cycle.
2. `pipeline_service.py:1630` — the exception is only **logged** (`exc_info=r`).
   Nothing persists it, which is why no table names the cause.
3. `pipeline_service.py:1359` — `save_analysis_result` runs *after*
   `run_v3_pipeline` returns. So a missing `analysis_results` row is proof the
   pipeline **raised** rather than returned.
4. `orchestrator.py:2127` — `check_ticker_complete` sits in the straight-line
   flow near the end of `run_v3_pipeline`, **not in a `finally`**. An exception
   before it skips all four per-ticker invariants. This is exactly why HOOD was
   invisible to them.
5. `orchestrator.py:1961` — the ORCL fix catches **`ValueError` only**, and it
   sits at the `PM_DONE` advance (line 1959). A desk stranded at `DEBATE_DONE`
   never reached line 1952, so broadening that handler alone would not help.

So the throw is between `advance_phase(DEBATE_DONE)` (line 1857) and line 1952 —
the PM/board leg, after the debate is already paid for. The container log is the
one place the exception type is recorded; `grep "Ticker .* failed"` on it names
the culprit and is the cheapest next step.

**Do NOT "fix" the `check_ticker_complete` gap** — I measured it and it is not
one. Those four per-ticker checks genuinely never run on the crash path, but
wiring them there emits, for the two real cases:

    desk exists, crashed  ->  PIPELINE_COMPLETE_BUT_NO_DECISION
    crashed before desk   ->  TICKER_ANALYSED_BUT_NO_DESK

The first is a row whose **name asserts something false** (the pipeline did not
complete, it died) and the second is strictly weaker than the
`phase_at_crash="NO_DESK"` + exception type `record_ticker_crash` already writes.
Two observers are only worth having when they can disagree; these would be
duplicates reading as corroboration. The reasoning is recorded in
`record_ticker_crash`'s docstring so it does not get re-litigated.

Beyond observability, nothing yet *recovers* the work: a ticker that dies after
the debate has been paid for still produces no decision. Whether it should be
retried, or salvaged into a degraded-provenance HOLD like the ORCL path does, is
a product call — `BOARD_DEGRADED_FALLBACK` already exists for exactly this shape.

> **Trap I nearly walked into — do not stamp `ABORTED`.** It is the obvious fix
> and it is wrong. `ABORTED` is terminal, so `DESK_STALLED_MID_PIPELINE` would go
> **silent** and the lost work would disappear behind a detector reporting
> health. `record_ticker_crash` therefore writes no `UPDATE` at all (asserted by
> test; the ABORTED mutation fails it), leaving two independent observers and
> preserving the dying phase as the only record of where the pipeline stopped.
> Muting your own detector with your own fix is the failure mode this whole line
> of work exists to prevent.

### 2. ~~Audit the rest of the suite for the MagicMock trap~~ — DONE, clean

Audited all **83** test files touching `get_db`. **The trap was confined to the
three live tests in `test_cycle_invariants.py`**, all fixed. A verified negative,
so the suite can be trusted on this axis:

- Two modules override the autouse `patch_get_db` — `test_db_constraints.py` and
  `test_connection_pool_exhaustion.py`. Both are deliberate and documented, and
  **neither reaches production**: the first uses the real *test* DB via
  `patch_real_get_db`, the second mocks `_ensure_pool` so no pool is ever opened.
- ~20 modules override `mock_db`. That is the intended composition — a locally
  configured mock, which conftest's autouse patch then installs.
- Integration tests use `real_db` / `patch_real_get_db` correctly.
- The heuristic scan's other 19 hits were false positives: they assert on
  *captured writes* (`cap["sql"]`, `cap["params"]`) or deliberately patch
  `get_db` to raise, to prove fail-open. Worth knowing before re-running a
  similar scan.

### 3. `SubagentStart`/`SubagentStop` + a hook registry

Unchanged from the previous handoff, but now cheaper: `make_pre_tool_hook` is the
second hook factory (after `_on_tool_result`), and a third would justify the
registry. A registry makes hooks declarative and individually toggleable — hence
ablatable via `gate_ablation.py`.

### 4. Cross-repo: tool attribution

Unchanged and still not mine to fix: `McpAdapter.ts:70` and
`ToolOrchestratorService.ts:1583` in **lazy-agent-service**. Note again that
`agent_tool_telemetry` already has full attribution — it is what let me measure
everything in this session — and the two tables are **not** redundant.

### Not open: item 4 of the previous plan (`DEBATE_ENGINE=1`)

**A concurrent session owns it**, in worktree `.claude/worktrees/debate-engine-off`
(pushed `9904dfb`, "pre-register the token saving so it can fail"). I stayed off
it deliberately. Do not double-implement.

---

## Traps

- **Every test gets a MagicMock database.** conftest's autouse `patch_get_db`
  means `get_db` is never real in pytest. Take the `live_db` fixture, and put a
  guard on the window — an audit that cannot fail is not an audit.
- **`on_tool_call` is called UNGUARDED by the SDK** (`lazycat/agent.py:320`),
  unlike `on_tool_result` which it wraps. A raise in a pre-hook kills the
  agent's turn, so the `try/except` in `make_pre_tool_hook` is load-bearing.
- **A non-None return from `on_tool_call` BLOCKS the call** and becomes its
  result. Returning something truthy by accident silently disables a tool.
- **`tool_schemas.json` is gitignored** and absent from a fresh worktree. Two
  allow-list guard tests skip without it; copy it from the main checkout. The
  skip reason says so, because a silent skip on the safety test is worse than
  the missing file.
- **The registry stores schemas WRAPPED** (`{"type":"function","function":{...}}`)
  while `tool_schemas.json` on disk is **flat**. `_schema_params` reads
  `schema["function"]["name"]`, so hand-populating `registry.schemas` with flat
  entries makes every lookup miss, `_filter_kwargs_to_schema` drop nothing, and
  every payload report ACCEPTED. My first oracle did this and "confirmed" a
  payload production had rejected. **Load via `load_from_json`.**
- **Truncating a payload while inspecting it can invent a bug.** My first
  whiteboard fixture omitted `section` because I had printed `args[:120]`. The
  real rows carry it, and the production `TypeError` named exactly **one**
  missing argument — which is how you know `ticker` was the only casualty.
  Check the arity in the error message against your fixture.
- **A repair makes its own defect invisible.** Once the hook injects the
  ticker, the call succeeds and leaves the failure telemetry entirely. Recording
  the repair is not bookkeeping; it is the only thing keeping the upstream
  bad-JSON bug measurable.

## Loose end (carried forward — still true)

`.claude/worktrees/fidelity-followup` is fully merged but still checked out; it
was this session's cwd, so it cannot remove itself. There is also a second live
worktree, `.claude/worktrees/debate-engine-off`, owned by the concurrent session.
Once both sessions are done:

    git worktree remove --force .claude/worktrees/fidelity-followup
    git branch -D worktree-fidelity-followup

Note `tool_schemas.json` was copied into the worktree so the allow-list guard
tests could run; it is gitignored, so it was not committed and disappears with
the worktree.

## Deployed — what is still unconfirmed

Deployed at `9fb797d`. The wait was deliberate: a cycle was live and actively
researching (`cycle-v3-1785386906`, LLY), and restarting the container strands
every in-flight desk — it manufactures the exact defect item 1 detects. Deployed
once `/api/v1/bot/cycle_running` returned `false` and that cycle's desk closed at
`PM_DONE`.

**Deployed: `195bf87` at 2026-07-30 07:50 UTC**, container healthy, and all of
the agent-cost flush, the stall-shape split and the repair hook verified live
inside it. Deployed twice this session, each time only after
`/api/v1/bot/cycle_running` returned `false`.

Cycles arrive irregularly — gaps of **1.5–6h are normal**, so a quiet two hours
is not a fault. Check `shared_desk` before concluding the scheduler broke; I
spent time chasing that and it was nothing.

A concurrent session also deployed over this one at 07:04. No harm — they build
from master, so my code was in their image — but **verify what is actually
running rather than assuming your deploy is the live one**: `docker inspect
--format '{{.State.StartedAt}}'` plus an `exec` import check settles it in
seconds.

**What is verified in the deployed image**: imports, allow-list size, `buy_stock`
excluded, a repair injecting a ticker end-to-end, `on_tool_call` present in the
harness construction.

**Verified on live traffic:**

- `DESK_STALLED_MID_PIPELINE` **fired for real** on NVDA at 07:28, catching a
  regression within minutes of it shipping — the first time this class of defect
  was caught automatically rather than by archaeology weeks later.
- The **agent-cost flush works**, with a clean before/after on live rows:

      after  NVDA  RESEARCH_DONE  5 telemetry rows   (mid-flight!)
      after  JPM   DEBATE_DONE    9 telemetry rows
      before HOOD/CARS/EXLS/OWL/UNH, same phases     0 rows each

  Rows existing while a desk is still non-terminal is the proof: the old single
  end-of-pipeline write could not produce that.

**Repair hook**: was INERT until `184758a` moved it onto the HTTP bridge (finding
6); deployed by a concurrent session at 08:11 UTC. Still unconfirmed on real
traffic — 0 repairs recorded, but also 0 malformed calls since that deploy, so
there has been nothing to repair.
`v3_invariant_violations` holds **0** `TOOL_ARGS_REPAIRED_PRE_FLIGHT` rows. I ran
one smoke repair inside the container and **deleted that row**, precisely so this
count stays honest — a synthetic row would make the query below read positive
with no real traffic. The 8 post-deploy tool calls are too few to conclude
anything either way.

### The queries that close this out (run after the next cycle)

Both features are new code paths on live traffic, so confirm they FIRE rather
than assuming silence means health:

```sql
-- the pre-hook should show repairs within a cycle or two (~6/day observed)
SELECT detail->>'tool', detail->>'agent', COUNT(*)
FROM v3_invariant_violations
WHERE kind = 'TOOL_ARGS_REPAIRED_PRE_FLIGHT' GROUP BY 1,2;

-- and the missing-ticker rejections should stop appearing here
SELECT created_at::date, COUNT(*) FROM agent_tool_telemetry
WHERE NOT success AND created_at > NOW() - INTERVAL '3 days'
  AND (error_message LIKE '%Required field%' OR error_message LIKE '%missing 1 required%')
GROUP BY 1 ORDER BY 1;

-- and the stall detector, on the next completed cycle
SELECT cycle_id, detail->>'count', detail->'stalled'
FROM v3_invariant_violations
WHERE kind = 'DESK_STALLED_MID_PIPELINE' ORDER BY created_at DESC;
```

If the repair count is **zero** after a full cycle, do not conclude it is
healthy — check that `enable_tools` was true for the agents that research, since
the hook is wired to that flag.
