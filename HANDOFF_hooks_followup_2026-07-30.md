# HANDOFF — hooks follow-up: items 1 and 2 of the previous plan (2026-07-30)

Continues `HANDOFF_harness_hooks_2026-07-30.md`. Two of its five open items are
done; the other three are re-ranked below with what I learned about them.

Merged and pushed: `cb35ab3` (item 1), `82067cb` (item 2).
**Deploy: see "Not yet deployed" at the bottom — this is the one thing left.**

Suite: **2,161 passed, 20 skipped, 2 failed.** Both failures reproduce on clean
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
That rules out the most convenient explanation and means the root cause is still
open (see item 3 below).

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

**5. The SDK already had the seam.** `AgentHarness` has accepted
`on_tool_call` all along; trading-service just never passed one. It also passes
the **same** `arguments` dict to `execute_tool` afterwards, so an in-place
mutation is what the tool receives. No SDK change — which matters, because
lazycat-sdk is shared with html-notes/canvas/music.

---

## What shipped

| change | evidence |
|---|---|
| 6th cycle invariant `DESK_STALLED_MID_PIPELINE` | reads `shared_desk`, not `analysis_results`; fires on 3/48 cycles (6/6 desks), silent on 45 |
| `live_db` fixture + guards on every live test | the `-k live` audit went from 2 failed + 1 hollow skip to **16 passed** against production |
| `PreToolUse` hook (`app/v3/tool_repair.py`) | injects the missing required `ticker`; both production rejection paths confirmed cured against the SDK's own validator |
| Repairs recorded as `TOOL_ARGS_REPAIRED_PRE_FLIGHT` | a repaired call vanishes from failure telemetry; without this the upstream bad-JSON bug goes invisible |

Terminal phases are **derived** from `_VALID_TRANSITIONS`, not duplicated, so
adding a `DeskPhase` cannot leave the check asserting a stale vocabulary.

### How both were validated

Not "the tests pass" — the previous handoff's own warning was that two of five
cycle checks passed a silence test and would have missed their motivating defect.

- **Positive replay**: the stall check fired on exactly the 3 of 48 cycles
  containing a stall, catching 6/6 desks.
- **Negative controls**: **83** INIT-only and **34** ABORTED-only cycles → 0
  fires. `INIT` is a legitimate triage skip; production ran 22 in a week, so
  firing on them would have made the check 79% false positives in week one and
  muted within days.
- **Mutation testing**: 9 mutations across both features (INIT no longer
  excluded, always-silent, unregistered, cap removed, `buy_stock` allow-listed,
  allow-list bypassed, overwrite-model-ticker, hook blocks, hook try/except
  removed). **Every one fails a test.**
- **Oracle, not re-implementation**: the repair is validated by loading
  `tool_schemas.json` through the registry's own `load_from_json` and asking its
  own `_filter_kwargs_to_schema`/`_schema_params`. It reproduces the exact
  production error strings before, and accepts after.

---

## Open work, in priority order

### 1. Fix the stall: an exception escapes `run_v3_pipeline` and nothing stamps the desk

I traced the mechanism; it is not a mystery any more, but I did **not** fix it —
see the trap at the end of this item, which is why it deserves its own change.

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

The fix is a `try/finally` around the per-ticker body that (a) stamps a desk
which is about to be abandoned, and (b) runs `check_ticker_complete` on the
exception path too.

> **Trap — do not stamp `ABORTED` and call it done.** `ABORTED` is terminal, so
> `DESK_STALLED_MID_PIPELINE` would go **silent** and the lost work would become
> invisible again, this time behind a detector that reports health. Whatever the
> stamp is, it needs its own violation kind (or must keep the dying phase in
> `cycle_metadata`, the way the existing `pipeline_incomplete` stamp does) so the
> rate stays measurable. Muting your own detector with your own fix is the
> failure mode this whole line of work exists to prevent.

### 2. Audit every remaining live/DB-touching test for the MagicMock trap

I fixed the four tests I touched. **I did not audit the rest of the suite.**
Any test that imports `get_db` and asserts on the result is asserting against a
mock — and passing. `grep -rln "get_db" tests/` is the starting list; the tell is
an assertion that would still hold if the query returned nothing.

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

## Not yet deployed

Everything is merged and pushed; **nothing is deployed.** A cycle was live and
actively researching (`cycle-v3-1785386906`, LLY, junior analyst mid-flight) and
deploying restarts the container, which strands every in-flight desk — i.e. it
manufactures the exact defect item 1 detects. Deploy once
`/api/v1/bot/cycle_running` reports `false`:

    curl -s http://10.0.0.16:3031/api/v1/bot/cycle_running
    ./deploy.sh

Both changes are inert until then: the invariant only records, and the pre-hook
only runs inside the container's agent loop.

### First thing to check after deploying

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
