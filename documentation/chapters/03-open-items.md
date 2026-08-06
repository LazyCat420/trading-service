# Open items

## 0. The analysts cannot emit their artifact — and the first diagnosis was wrong

> **RETRACTED 2026-08-05, same day.** Everything below the line was written on
> the strength of a `[THINK-LEAK]` alarm, and the alarm was a **false
> positive**. Measured afterwards: **43 of 43** chat calls reached the
> vllm-shim carrying `enable_thinking=false` (`absent=0`), the shim mirrors it
> onto DeepSeek's `thinking` key, a direct probe with the flag returns
> `reasoning_content: null`, and the model reports
> **`reasoningOutputTokens: 0`**. The model is not reasoning. The thinking-off
> flag works end to end.
>
> What the canary actually matched was the junior analyst's own prose — *"Let
> me trace the most load-bearing lead. The key story here is the AI capex…"*.
> Its regex fires on any answer opening "Let me…", and its error message
> asserts a cause it cannot observe. That assertion was believed and written
> up here as fact. The canary now requires usage evidence before accusing
> (`reasoning_tokens == 0` → not a leak); see `test_think_leak_canary.py`.
>
> **The artifact loss is real and still open.** What is refuted is the
> mechanism, not the symptom. The evidence points instead at agents exhausting
> their tool-turn budget and ending on narration or nothing —
> `took too much time (216.6s) over 5 tool turns without completing`,
> `outputTokens=4097` with `content` empty. Diagnose that next, from the
> head/tail logging added at the unparseable-artifact site, and **do not
> re-open the thinking-flag theory without new evidence**.
>
> Lesson worth keeping: a tripwire that names a root cause in its message is
> making a claim, and a claim needs evidence the tripwire can actually see.

### The actual mechanism, captured 2026-08-05

The head/tail logging added at the unparseable-artifact site answered this
within one cycle. Two shapes, and the common one is not subtle:

**1. The agent narrates its next step and never emits.** This is the majority.

```
Failed to parse artifact from v3_junior_analyst output (108 chars)
  HEAD: I'll complete the analysis and emit the desk_note JSON based on
        the pre-collected data and prior research.

Failed to parse artifact from v3_quant_analyst output (9113 chars)
  HEAD: ... Let me run a backtest/equation to test the mean-reversion setup
        on TMUS ... Let me check the library for a suitable equation and run one.
```

The model announces what it is about to do, the turn budget ends, and the
harness returns that announcement as the agent's answer. The agent did the
work — it is mid-flow — it simply never reached the JSON. This is the same
event the `ManagerAgent` reports as `took too much time (216.6s) over 5 tool
turns without completing`, seen from the other end.

**2. Braces present, still unparseable.** Rarer:

```
Failed to parse artifact from v3_quant_analyst output (4466 chars)
  HEAD: { "summary": "META at $588.77 sits in a confirmed bearish ...
  TAIL: ..."#no_reversal", "#quant_only"] }
```

Opens `{`, closes `}`, and still fails — so this is malformed JSON inside the
body (an unescaped character or a dropped delimiter), not truncation. Worth
sampling separately; do not lump it with shape 1.

**Why the repair pass does not save shape 1.** `agent_runner` already retries
tool-lessly, but it hands the model only `final_text[:2000]` — the *narration*
— plus the original prompt. The agent's actual work lives in its tool results
and intermediate turns, and none of that reaches the repair. So the repair
asks for a report from material that does not contain one, and frequently
fails too (`4134 chars`, unparseable again).

**Fix candidates, cheapest first — none applied yet, and the choice matters:**

1. **Give the repair the agent's own work — APPLIED 2026-08-05.** `run_agent`
   now accumulates a bounded transcript of tool results (12 entries × 1,200
   chars, cleared per retry so a repair is built from the attempt that actually
   failed) and returns it; the repair prompt leads with *"WHAT YOU ALREADY
   FOUND"* before the rejected attempt. Touches no agent prompt, so it stays
   clear of the confidence measurement window, and the repair runs tool-less so
   the schemas that dominate the first call are already gone.
   **Unproven** — it needs a cycle to show whether repairs now succeed. Watch
   for `repairing <TICKER> with N recovered tool result(s)` followed by fewer
   `produced no parseable artifact` lines.
2. **Tell the model when it is on its last turn.** The output directive sits in
   the user prompt from turn one; nothing signals "emit now or lose the work".
3. **Raise the turn budget.** Simplest, least targeted, and it buys time rather
   than fixing the shape — an agent that narrates at turn 5 can narrate at
   turn 8.

Prefer (1): it recovers work already paid for, and it is the only one of the
three that cannot make the cycle slower.

### Original entry, retained for the record

**Impact: the research layer.** Analyst artifact failures went from **0%
through 08-03** to 22-36% on 08-04 and after. This is the cause of the
"no valid artifact produced" events, and it is **not** a logic bug in any
agent — it is a model/flag mismatch one service away.

**Root cause, captured live 2026-08-05** by the empty-response instrumentation
added that morning:

```
[THINK-LEAK] v3_valuation_analyst: response content starts with a reasoning
  trace ('Let me get memory peers specifically (MU, WDC, STX)...').
  The thinking-off flag is not reaching the model.
EMPTY RESPONSE from v3_valuation_analyst (AMD): raw='' |
  model=deepseek-v4-flash-0731 provider=vllm-2 |
  usage={'inputTokens': 40042, 'outputTokens': 4097, ...}
```

Gold Spark swapped to `deepseek-v4-flash-0731` on 07-31. Prism's thinking-off
flag uses the **Qwen spelling (`enable_thinking`), which DeepSeek silently
ignores**, so reasoning runs on every call. The model then spends its whole
output allowance thinking — 4,097 output tokens with `content` returning
**empty** — and never reaches the JSON. Where some reasoning does land in
content, the artifact parser sees prose, not an artifact.

Note the shape: this is *not* the same as an agent being slow. Per-loop time
barely moved (53s → 63s); what changed is that analysts now burn 4-6 loops and
frequently end with nothing to show.

**The real fix is the vllm-shim in `lazy-agent-service`** — it must send the
DeepSeek-compatible thinking-off parameter instead of the Qwen one. That is a
different repo and is not fixed here. `strip_reasoning_leak` is only a
tripwire, and its salvage cuts to the first *markdown heading*, so it can
never recover a V3 agent's **JSON** artifact.

**Mitigations that ARE in this repo (2026-08-05):** the unparseable-artifact
log now records the head and tail of what came back, not just a character
count — a 49-char failure and an 11,248-char failure are different bugs and
the count could not tell them apart.

**Do not "fix" this by trimming prompts or raising retries.** Both leave the
model reasoning into a budget that has no room for the answer.

## 1. The gatekeeper LLM returns empty responses — ROOT-CAUSED 2026-08-06

> **RESOLVED, and the hypothesis below was wrong.** It was not the tool
> payload. Prism injects `minP: 0.05` whenever the caller omits the field, and
> a vLLM box running speculative decoding refuses any `min_p > 0` — raising it
> *inside the stream generator, after answering HTTP 200*, so prism sees an
> empty stream rather than an error. Fixed by forwarding `min_p=0.0`
> (lazycat-sdk `0.3.10` + `base_agent.min_p_for`); full write-up in
> `04-incidents.md`. Two supporting claims below are also retired: the tool
> floor re-measured at **83 tools / ~21k tokens** (not 275/91k), leaving
> 38,179 output tokens on the Jetson, and `enable_tools=False` never removed
> tools at all — it is client-side only.

## 1b. The Jetson has processed almost nothing since 2026-06-25

Found while building the benchmark, and invisible on any live metrics page:

| source | volume | window |
|---|---|---|
| `llm_audit_logs` (jetson) | **12,720 calls** | 2026-06-06 → **06-25**, then nothing |
| `v3_agent_telemetry` (`provider='vllm'`) | **0** | ever |
| `model_shadow_runs` (jetson) | 21 SUCCESS, 1 error | 08-04 → 08-06 |
| vLLM lifetime counters | 127 requests / 1.52M prompt tok | 50.1h uptime |

The box served thousands of calls a day in June, went dark on **06-25**, and
has run at **~2.5 requests/hour** since. The `minP` fix removes the reason no
v3 agent could reach it, but nothing yet *routes* work there — so this stays
open until a role is deliberately assigned and its results measured.

Note the measurement trap: vLLM's counters are **since process start** and do
not survive a restart, so the six-week gap is only visible in the database.
`scripts/jetson_benchmark.py --phase inventory` snapshots both into
`box_benchmark_runs` so the history accrues instead of resetting.

## 1c. (superseded) The gatekeeper's empty responses — original diagnosis

`v3_portfolio_manager` (`deepseek-v4-flash-0731` on `vllm-2`, via prism
`/agent`) intermittently returns nothing: failed 08-04 22:28, succeeded 23:00,
failed 08-05 06:31 and 11:11. Selection currently comes from the scoring engine
fallback, so the gatekeeper's qualitative judgement — catalyst quality,
mega-cap balance, freshness overrides — is **not being applied at all**.

The call passes `enable_tools=False` to get clean JSON, yet still goes through
`/agent`, which carries a large tool payload on this project. It may be paying
that cost for nothing.

**Next step:** capture the raw prism response for one failing call. The current
log records only that content was empty, never what actually came back. Decide
between `/chat` and a model known to survive `/agent` *after* that evidence
exists, not before.

**2026-08-05: the capture is now armed.** `base_agent.run_agent` logs raw
content, resolved model/provider, token count, elapsed time, loop count and
usage at the empty-response site before substituting the sentinel. The next
failure self-documents; look for `EMPTY RESPONSE from v3_portfolio_manager`
in the container logs. Supporting fact for the tool-payload hypothesis:
`CUSTOM_V3_PORTFOLIO_MANAGER` registers prism-side with 13 tools the
gatekeeper never uses.

## 2. Agents exhaust their tool-turn budget

Twelve occurrences in the first two hours of one cycle:

```
ERROR [ManagerAgent] Agent v3_fundamental_analyst took too much time
(207.6s) over 7 tool turns without completing.
```

These agents are working — the NYT report quoted in *Current state* came from
one — but several run out of budget before finishing. This is a plausible
contributor to the cycle's slow wall-clock, and it means some tickers get a
truncated analysis rather than a complete one.

Unclear whether this is new or was simply invisible while every agent failed
earlier. **Needs a baseline before it is treated as a regression.**

This is now the main constraint on cycle throughput: 32 agent completions in
two hours, against 13 tickers that each traverse the full 4+1 layer stack.

## 3. A reserved-section write is blocked but reports success

```
[WhiteboardTool] BLOCKED write to reserved section 'fundamental_report'
  by agent 'v3_fundamental_analyst' (NYT)
[ToolRegistry] Tool Execution: whiteboard_write by unknown - SUCCESS (62ms)
```

The agent produced a complete, well-formed report, the write was refused, and
the tool returned **SUCCESS** anyway. The agent has no way to learn its output
was discarded, so it cannot adapt — and in this instance it went on to exhaust
its time budget.

A refusal reported as success is the same failure shape as the gatekeeper bug:
the caller cannot distinguish "done" from "silently dropped". Either the
section should be writable by that agent, or the tool must return an error the
agent can act on.

## 4. No enforcement against a second claiming instance

Any process reaching the shared database can claim a queued cycle. Worker
identity now makes this diagnosable in one log line; it does not make it
impossible.

**Deliberately not enforced.** An "active worker" flag that non-matching
instances refuse risks a far worse failure — a misconfigured flag silently
stops every cycle with no error to notice. Observability first; enforcement
only if this recurs.

## 5. `pipeline_state` can strand a cycle at `running` — FIXED 2026-08-05

The auto-clear judged staleness on `started_at > 30min`, which failed both
ways: a crash less than 30 minutes before the next scheduled command made a
healthy instance skip a cycle it could have run (the 2026-08-05 case), and a
legitimately long cycle could be force_reset out from under a live owner.
Staleness is now judged on `updated_at`, which `save_state()` stamps on every
cycle event emit — a real heartbeat. Threshold: 15 minutes without any state
write. See *Current state* for the mechanism.

## 6. Log noise that trains the eye to ignore logs — FIXED 2026-08-05

All three silenced at the correct layer; see *Current state*. The Twitter
sweep is now gated behind `TWITTER_SWEEP_ENABLED=False` — flip it the day
scraper-service gets `TWITTER_ACCOUNTS` credentials, or the collector stays a
deliberate no-op.

## 7. A blocked trade was still scoreable as a kept one — FIXED 2026-08-06

The 2026-07-31 fix gave the policy gate's veto a label. `decision_outcomes`
deliberately keeps `action='BUY'` on a blocked trade — its P&L is the
counterfactual, and that is how the confidence floor gets back-tested — so
`overridden_from` is the *only* thing separating a refused trade from one the
desk kept. `override_scorecard()` buckets on it.

The label was read from one table, `trade_results.policy_action`. **That row is
not always written.** Measured 2026-08-06 across all 25 policy blocks on
record: **8 had no `trade_results` row at all**, so the lookup returned NULL and
the block was recorded as an allowed trade. **Five had already been graded** —
`ASML` (×2), `COF`, `ASIC` as WIN, `CRH` as LOSS — crediting the desk with P&L
on trades the floor refused. This is the exact failure the 2026-07-31 fix was
written to end; it ended it for the blocks that happened to have the row.

`BLK` and `FCF` were blocked in the *same cycle* (`1785991713`) and only `FCF`
was labelled. That is the shape to remember: a discriminator with a single
source, whose absent state is indistinguishable from "no block".

The gate is now read from **two** independent records — `trade_results` and
`v3_guardrail_firings`, the latter written by the guardrail on the same path
that refuses the trade, and present for all 25. Either naming a block is a
block, and each lookup is separately fault-tolerant so an unreadable table
cannot suppress the other's evidence. A missing row must never read as
permission.

**The test could not have caught this.** `test_blocked_decisions_are_labelled`
re-implemented the resolver's branch logic inside the test file and asserted
against the copy, so production was free to diverge from it — and had.
It even asserted the bug: `_resolve("BUY", None) is None`, which is precisely
the shape of a block with no `trade_results` row, was pinned as correct
behaviour. The tests now call `resolve_overridden_from()`, the function the
recorder itself uses, and one of them pins that wiring.

**Backfilled 2026-08-06.** The code fix only protects new rows, so the history
was relabelled from `v3_guardrail_firings` by
`scripts/backfill_blocked_decision_labels.py` (`--dry-run` by default). Seven
rows carried the gap — three of them `HOLD_POLICY_BLOCKED_MISSING_REGIME`
rather than `LOW_CONFIDENCE`, which is why the guardrail match is a prefix:

```
CRH   cycle-v3-1785120233        BUY@75  LOSS -4.87%   MISSING_REGIME
ASIC  cycle-v3-1785120233        BUY@85  WIN  +5.21%   MISSING_REGIME
COF   cycle-v3-1785120233        BUY@75  WIN  +3.04%   MISSING_REGIME
ASML  cycle-observe-1785396275   BUY@65  WIN  +8.22%   LOW_CONFIDENCE
ASML  cycle-observe-1785397223   BUY@65  WIN  +8.22%   LOW_CONFIDENCE
RIVN  cycle-v3-1785739018        BUY@60  ungraded      LOW_CONFIDENCE
BLK   cycle-v3-1785991713        BUY@68  ungraded      LOW_CONFIDENCE
```

All 25 policy blocks on record are now labelled; none are unlabelled. Only
`overridden_from` was written — `action`, `outcome` and `pnl_pct` keep their
meaning as the counterfactual, so the four WINs and one LOSS above are still
available to back-test the floor with. They are simply no longer counted as
trades the desk chose to keep. The script writes
`backfill_blocked_labels_undo.json` before touching anything.

## 8. The retry contract held in one branch and not its neighbour — FIXED 2026-08-06

`should_abort()` kills the whole ticker on a second `AGENT_ERROR`, so the
2026-07-26 audit set a rule: an artifact failure returns `AGENT_ERROR` on the
first attempt to earn the retry, and degrades to the non-fatal `DATA_GAP` on
the retry itself. The collapse branch in `run_v3_agent` honours it. **The
"no parseable artifact" branch, twelve lines above it, ignored `is_retry`
entirely.**

That branch was unreachable on a retry by accident rather than by design: a
truncated artifact used to parse into its own nested block, `_is_wrong_shape`
caught it, and the fragment was restored so the collapse branch graded it. Then
the parser stopped handing the nested block back — correctly; salvaging it
manufactures fields the model never emitted and burns a ~100s tool-enabled
re-run rediscovering research. It returns `{}` now, `_parse_artifact` reports
that as `None`, and so `fragment` is `None` too. The restore path had nothing
to restore, and a truncated artifact took the hard branch on **both** attempts
and aborted the ticker.

The degrade now lives in the branch itself rather than only in the path that
used to feed it. Two red tests were the visible symptom and both are green:
`test_nested_fragment_is_a_parse_failure` was asserting the contract and
failing against production, and `test_truncated_outer_salvages_inner_fragment`
was a stale assertion of the salvage behaviour that had been deliberately
reversed — it now pins `{}` and says why.

Worth stating plainly, because the wave that shipped these fixes did not run
the suite: **both failures were on `master` and both described live
behaviour.** A red test here is not noise.
