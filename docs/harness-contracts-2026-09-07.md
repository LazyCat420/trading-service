# Decision, evidence, and research lifecycle contracts

New desks carry decision contract version 1. Historical artifacts remain readable
without fabricated entry intent or attribution. Board and synthesis both receive
the typed contract outside prose truncation. Synthesis names the Board hash and
original action. Changes to action, size, stops, target, exit style, or entry intent
require a reason and a named evidence source. Timing changes require their own
reason. Invalid output earns the existing retry and cannot authorize an order.

`enter_now` permits an immediate order after all existing policy and sizing gates.
`enter_on_condition` arms a re-analysis trigger and never buys immediately.
`watch_only` never buys. A later wake must verify the condition and produce a fresh
`enter_now` decision; a trigger is not an order authorization. Explicit null clears
a trigger rather than resurrecting the Board field. Registration outcomes are saved.

Delta may complete only a valid HOLD. BUY and SELL escalate through the full panel
and regime gates. `analysis_mode=full` bypasses freshness shortcuts and junior
triage; existing panic/debate gates and execution policy remain binding. Glances and
degraded desks cannot refresh the timestamp of a previous substantive analysis.
Material wakes bypass glance, and unknown news counts cannot establish no news.

The compressed context retains complete defense answer/concession records within
4,500 characters, with omission markers directing readers to the full artifact.
Total compressed context remains at most 10,000 characters. Prompt hashes and
contract/defense delivery receipts are persisted and supplied to autoresearch.
A missing receipt is unverified. A one-turn Delta HOLD is a supported route, and
historical prediction cohorts are identified separately from current cycle health.

Versioned decisions may specify an observable resolution condition. Baseline watch
writers and readers preserve it. News uses the matched source timestamp and complete
title; unknown timestamps cannot pass freshness checks. Allocation outcomes are
scored idempotently at cycle completion and during watch sweeps, joining the wake
command to the actual cycle. Retaining a position is distinguished from a changed
action and risk-level updates. The allocator defaults to enforcement after frozen live-trip replay and real Mongo
watch-sweep validation. Explicitly stored operator overrides still win, and mode 1
remains the shadow rollback. The optional model planner remains disabled.

A dynamic universe reserves at most one existing slot for queued work. Explicit
ticker requests keep their names. Each ticker claims at most three questions and
runs full research with those questions in the prompt. Leases renew every minute;
completion uses the owning cycle and lease token. An answer requires an exact quote
from the actual delivered prompt or recorded tool transcript. Unresolved work is
deferred twelve hours and eventually fails after the existing attempt limit.
Answers use a durable `answer_ready` outbox; ledger delivery is idempotent and a
failed write never marks research completed. Crashed workers cannot finish newer
claims. No separate model loop or new order path is introduced.

Validation includes production prompt assembly/parser tests, six production executor
AST cases with isolated I/O, and disposable Mongo lifecycle tests. Test records live
under the workspace `.scratch/harness-fixes-20260907` evidence directory. Deployment
and the subsequent live cycle are audited separately so deterministic coverage is
not confused with observed model or market behavior.
