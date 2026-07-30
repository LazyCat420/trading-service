# HANDOFF — tool attribution: half-fixed, and the other half is cross-repo

**`trading-service@a93c4e9` merged, pushed, deployed, healthy.**
Suite: 2,100 passed, 19 skipped, 1 failed (the long-standing
`test_whitelists_grant_write_to_pm_and_board_only`, unchanged).

---

## Why this mattered

The cycle spends **128M tokens over 7 days — ~1.2M per ticker — to make ~6
external data lookups per ticker.** 31% of all tokens go to
`v3_tournament_debate`, which runs at `loops=1.0`, i.e. it makes **zero tool
calls**. Before rebalancing spend toward actual research, you need to know which
agent researches. You couldn't: **every row in `tool_usage_stats` was
unattributable.**

---

## What is genuinely fixed

### `cycle_id` — VERIFIED live, 27/27 new rows

The INSERT in `app/tools/registry.py` omitted `ticker` and `cycle_id` entirely,
even though both columns exist. Now written. `cycle_id` resolves through
`current_cycle_id()`, which falls back to the running-pipeline singleton — so it
works **without** needing anything from the caller, which is exactly why it is
the part that now works.

`current_cycle_id()` returns the literal `"default_cycle"` when it can't
resolve one; that is mapped to NULL rather than stored, because a sentinel would
invent a cycle that never ran and pool unrelated calls under one id.

### `get_sec_filings` — schema fixed, live evidence still thin

14 of 53 calls (26%) were rejected **before reaching the function**:

```
Malformed arguments: missing ['ticker']
```

The SDK lowercases keys, drops every undeclared one, and only *then* checks
required (`lazycat/tool_registry.py:534-551`). Agents carrying the old
EDGAR-style schema send `action/cik/limit/symbol` — all dropped as undeclared,
leaving `ticker` unset. The `**_extra` catch-all in the function was written for
exactly these strays but never got to run, because rejection happens a layer
above it.

Fixed by declaring `symbol` so it survives filtering, dropping `ticker` from
`required`, and resolving in order: explicit arg → alias → pipeline ticker.
Reproduced before and after: a call with only `{action, cik, limit}` now returns
real data instead of failing.

**Since deploy: 1 call, 0 failures, and zero `Malformed` errors across all
tools.** n=1 is not evidence. Re-check after a few cycles.

---

## What is NOT fixed — and cannot be, from this repo

**`agent_name` is still `'unknown'` and `ticker` is still NULL on new rows.**

I initially verified this fix with a synthetic probe that set the tool context by
hand. The probe proved the INSERT works. It did **not** prove the context gets
populated in production, and in production it does not. Stating that plainly
because the earlier claim was too strong.

The caller does send these fields — `lazy-agent-service`'s `LocalToolRouter.ts`
POSTs `agent_name` / `ticker` / `cycle_id` in the JSON body — but they arrive
empty, because **its own context is empty at two call sites**:

| call site | passes |
|---|---|
| `src/services/McpAdapter.ts:70` | `routeLocalTool(toolName, toolArgs)` — **no context at all** |
| `src/services/ToolOrchestratorService.ts:1583` | `agentName` + `cycleId`, **never `ticker`** |

So MCP-routed tools get zero attribution, and orchestrator-routed tools never
get a ticker. That matches the data exactly: `ticker` NULL always, `agent_name`
named only occasionally.

**The fix is two lines in `lazy-agent-service`** — pass a context from
`McpAdapter`, and add `ticker` to the `ToolOrchestratorService` context object.
Not done here: it is a different repo and a shared service (html-notes, canvas
and music tools route through the same file), so it deserves its own change and
its own verification rather than being smuggled into a trading-service commit.

### This is a regression, not a permanent state

Attribution used to work and decayed:

| week | rows | named agent | ticker |
|---|---:|---:|---:|
| 06-01 | 892 | 892 (100%) | 892 |
| 06-22 | 1,751 | 1,751 (100%) | 1,751 |
| 07-13 | 3,117 | 1,211 (39%) | 3,117 |
| 07-20 | 1,566 | 37 (2%) | 1,566 |
| 07-27 | 1,091 | 7 (0.6%) | 1,061 |

Worth bisecting `lazy-agent-service` across that window — something stopped
threading context through in mid-July, and whatever it was probably explains
both columns at once.

---

## Next, in order

1. **Fix the two `lazy-agent-service` call sites.** Cheap, and it unblocks
   everything below — you cannot rebalance research spend you cannot attribute.
2. **Re-check `get_sec_filings`** once n is meaningful (was 26% failing).
3. **Rebalance the budget.** `v3_tournament_debate` is 31% of tokens at
   242k/run with zero tool calls. It *ranks* well (AUC 0.608, p=0.0072 — keep
   the signal), but the probabilistic panel is already built and wired behind
   `DEBATE_ENGINE=1`. Switching frees ~40M tokens/week.
4. **Spend the freed budget on research loops**, gated by an
   evidence-completeness check ("what haven't I checked?") rather than a fixed
   loop count. Only `junior_analyst` (5.4 loops) and `fundamental_analyst`
   (5.8) call tools today; everything else sits at ~1.0.

## Where the tokens go (7 days, 106 tickers)

| agent | tokens | % | loops |
|---|---:|---:|---:|
| v3_tournament_debate | 39.7M | 31% | **1.0** |
| v3_junior_analyst | 32.5M | 25% | 5.4 |
| v3_fundamental_analyst | 31.3M | 24% | 5.8 |
| quant / board / synth / valuation / regime | 24.5M | 20% | ~1.0 |

Tool calls are 40% whiteboard coordination, 59% external research — **6 external
lookups per ticker**, or roughly one lookup per 200k tokens.

## Loose end

`.claude/worktrees/fidelity-followup` is fully merged and still checked out; it
was a session cwd so it could not remove itself. `git worktree remove --force`
it and delete branch `worktree-fidelity-followup`.
