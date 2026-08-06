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

## 1. The gatekeeper LLM returns empty responses

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
