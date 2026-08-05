# Open items

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

## 2. Agents exhaust their tool-turn budget

Seven occurrences in one cycle:

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

## 5. `pipeline_state` can strand a cycle at `running`

A process that dies mid-cycle leaves `status='running'`, and the next scheduled
command is skipped with `Pipeline state is stuck at 'running' from a previous
crashed cycle`. Orphan detection exists but on 2026-08-05 did not clear the
state before the next scheduled cycle was due. Worth confirming the auto-clear
threshold is shorter than the scheduling interval.

## 6. Log noise that trains the eye to ignore logs

- `$SKHYV: possibly delisted; no price data found` — logged at **critical**,
  15× in 3.5 hours. A delisted ticker is not a critical event.
- `Twitter accounts sweep stored 0 tweets across 16 accounts` — backend
  unconfigured; configure it or stop sweeping.
- `[SCHEDULER] Job sch-bot-<id> has no next_run_time` — recurs with no visible
  consequence; either it matters or it should not be a warning.
