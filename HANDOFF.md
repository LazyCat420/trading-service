# HANDOFF — Report audit, the bugs it found, and an adversarial test suite (2026-07-25)

Shipped `ea0721e` · `304ecbe` · `2309aef` · `3b4d533`. Previous wave's handoff
archived to [`docs/HANDOFF_quant_factor_wave_2026-07-25.md`](docs/HANDOFF_quant_factor_wave_2026-07-25.md).
Full audit: [`../.agents/AUDIT-report-verification-and-fixes-2026-07-25.md`](../.agents/AUDIT-report-verification-and-fixes-2026-07-25.md).

---

## What this wave was

Verify the four 2026-07-25 reports against the actual code, fix what was broken,
then build a test type this repo never had: **feed the checkers bad data and see
whether they actually fire.**

**The reports were honest.** All 12 cited commit hashes resolve with matching
messages, and most claims verified. Every gap below is something they did not
know, not something they misrepresented — including their own retractions, which
were accurate.

---

## THE HEADLINE

**The decision-integrity fix shipped with a crash in the code that reads its own
sentinel.**

```python
>>> {'action': None}.get('action', 'HOLD').upper()
AttributeError: 'NoneType' object has no attribute 'upper'
>>> {}.get('action', 'HOLD').upper()      # contrast: missing key is fine
'HOLD'
```

The wave added `{"action": None}` to mark a degraded board. A dict default only
fires on a **missing** key, not a null value. So the desk *engineered to record a
degrade* was the one that crashed and got swallowed by
`gather(return_exceptions=True)`.

**Why it shipped, and this is the transferable lesson:** the sentinel was tested
in `test_decision_provenance.py` ("is it written?") and the gate in
`test_policy_gates.py` ("does it gate a string?"). **Two correct tests in two
files that never meet do not test the path between them.** The repo said so
itself — *"the degraded-sentinel path has only ever been unit-tested."*

---

## What the NEW TESTS found that reading the code did not

This is the part worth internalising. The audit found bugs by reading; the
fault-injection suite found five more by feeding garbage in — including one the
reports believed was already fixed.

| Bug | Severity |
|---|---|
| **NaN confidence passed every gate.** Every comparison against NaN is `False`, so `confidence < floor` read as "cleared the floor" — the low-confidence gate silently inverted | **P0** |
| **A garbage action produced the label `EXECUTE_3.14`** — an order authorized by unparseable input | **P0** |
| A non-string action (`3.14`, `True`) crashed the gate; the first fix only handled `None` | P1 |
| **The fail-open composition bug was never actually fixed** (below) | P1 |
| `_z_score` zero-filled a degenerate cross-section — fabricating "perfectly average", the one thing its own docstring forbids | P2 |

> ⚠ **The NaN one is the most dangerous thing in this wave.** The audit's own
> traps section warns "sanitize NaN where values are consumed, not only where
> fetched" — and the gate consumed it unsanitized. No amount of code reading
> found it; one parametrized test did immediately.

---

## The skill loop was churning, not learning (`0f6266b`)

Asked "are the agent skill docs actually being improved, or just replaced?" —
measured against the live `agent_skills` table, the answer was **replaced**:

- **137 of 145 versions are `REPLACE`.** No `SKIP` was ever stored, despite the
  prompt saying "SKIP is the correct default".
- **6 of 7 accepted edits scored exactly `+0.0150`** — the scorer's maximum —
  and **all 66 rejections scored exactly `-0.0050`**. Two values, so it ranked
  nothing.
- One accepted version's *only* change was renaming a bullet with a
  byte-identical body.
- Fed a genuine edit and deliberate keyword soup, it scored **the soup higher**.

Three causes: `MAX_SKILL_CHARS` was 4000 while the prompt said 1500 (unenforced
limit → 1146→1812 char bloat); the near-noop check compared *whole-doc*
similarity at 0.95 while real edits ran 0.84–0.94, so it never fired; and the
scorer rewarded surface features any rewrite satisfies. Now: bullet-level
comparison with labels stripped, structural rejection separate from scoring,
proportional credit with bloat/repetition penalties. **Real history re-judged:
7/7 → 1/7 accepted.**

> ⚠ **Honest limit:** this scores whether a doc is better *written*, not whether
> it makes better *trades*. `decision_outcomes` has 2028 resolved rows but **no
> `agent_name` column**, so per-agent accuracy is unattributable without a schema
> change. The docstring now says so instead of implying otherwise.

## Making the skill loop falsifiable (`8d12235`)

Follow-up to the gate fix, from the obvious next question: *how do we know the
skill edits produce better trading?* Answer as of this morning: **you couldn't**,
and the reason was structural, not a missing report.

- `agent_skills` had 145 versions, `decision_outcomes` 2028 resolved rows, and
  **0 rows joined the two**. `agent_skills.cycle_id` is the cycle that *produced*
  an edit, not the cycles it later governed.
- The board took **20 versions in ~5 days** against a **7-day resolve horizon**,
  so every version was replaced before one of its trades matured. n=0 per
  version, permanently.

Now shipped: `decision_outcomes.skill_versions` (JSONB, stamped from the same
cache entry the prompt was built from), a **25-resolved-decision maturity gate**
before a version may be replaced, and
[`scripts/skill_version_scorecard.py`](scripts/skill_version_scorecard.py) —
per-version win rate and avg P&L with Wilson intervals against the always-long
baseline.

> ⚠ **This makes the loop falsifiable, not proven.** A version will govern ~25-60
> decisions; detecting a ~1% edge needs hundreds, and this repo's own
> residual-alpha work found none at n=106 (t=-0.904). Expect "not
> distinguishable" for months. The value is being able to say the loop **isn't
> hurting** — and having grounds to switch it off if it never shows anything.
> A real answer needs an **A/B**: two bots, different versions, same tickers,
> same cycles. Not built.

## Traps (will bite again)

- **Zero and unknown are different, and confusing them freezes the fleet.** Zero
  stamped rows means EITHER "brand new version" (hold it) OR "predates the stamp"
  (unknowable). Every live version on 2026-07-25 was the second kind, so treating
  zero as zero would have held all seven agents for weeks after deploy.
  `_decisions_governed` distinguishes them by asking whether the stamp is flowing
  at all, and returns `None` — edits proceed — when it is not. Same rule as the
  provenance work: **missing must never be defaulted into a confident value.**
- **Record the version SERVED, not the newest one.** The optimizer can accept a
  new version mid-cycle while a process serves the cached older one for up to the
  TTL. Reading "current version" from the DB at record time would attribute
  trades to a doc that never ran. `active_skill_version()` reads the same cache
  entry the prompt was built from.
- **An edit cadence faster than the measurement horizon is unfalsifiable by
  construction.** No instrumentation rescues n=0. If something is edited once per
  cycle and evaluated on a 7-day lag, the edit rate is the bug.

- **A limit the code does not enforce is a suggestion.** `MAX_SKILL_CHARS=4000`
  vs a prompt saying 1500 is why the docs bloated for 20 versions with nothing
  objecting. Quote the constant in the prompt; never hardcode the number twice.
- **Whole-doc similarity cannot detect a rename.** Renaming one bullet in eight
  barely moves the ratio. Compare at the unit a human would call "a change" —
  here, bullet bodies with the label stripped.
- **Tightening a limit can FREEZE what is already over it.** Dropping the ceiling
  to 1800 would have trapped the 5 live docs above target forever: every
  candidate near their size gets rejected, so they can never shrink. Any
  tightening needs an explicit path back under the line.
- **A gate that emits two values is not a gate.** If every accept scores the max
  and every reject the same min, it is a coin, not a filter. Check the
  *distribution* of a scorer's outputs before trusting it.

- **⚠ A HEALTHY HTTP ENDPOINT IS NOT A HEALTHY CONTAINER.** `4517ba1` was a
  hotfix for a bug *this wave introduced*, caught minutes after deploy by
  watching the container instead of trusting the deploy's exit code. The new
  bulk technicals pass ran 507 recomputes back-to-back at boot, pinned CPU at
  ~86%, and timed out Docker's 10s healthcheck three times → **UNHEALTHY, while
  `/health` was answering in 0.02s.** The app was fine; the event loop never got
  a turn. Any `await`-less loop over hundreds of CPU-bound items does this. Yield
  with `await asyncio.sleep(0)` between items, bound each item, and cap the pass.
- **"~0.1s each" is not the number that matters.** Each recompute is individually
  fast, which is exactly why the loop looked safe. The cost was in the *absence
  of gaps* between 507 of them. Multiply, then ask what else needs the loop.

- **⚠ A DOCUMENTED LESSON IS NOT AN IMPLEMENTED FIX.** The last HANDOFF describes
  the fail-open composition trap at length and states the rule — *"fail-open
  composition is not free"*. But the fix it shipped (timeout 25s → 60s, cache the
  HMM) only made it **less likely**. Every component still ran under ONE outer
  timeout, so any slow one still evicted GARCH, HRP and the sizing bracket. A
  chaos test that hangs a component proved the block still died whole. Each
  component now has its own deadline, enforced **on the call**, not merely checked
  before it — a pre-call budget check cannot stop something that starts just under
  budget and then hangs.
- **A cache keyed by the wrong thing is worse than no cache.** The HMM cache was
  keyed by **calendar date** while every document describing it — including its
  own docstring, which claimed parity with `regime_cache.py` — said per-cycle. At
  ~8-9 cycles/day the first cycle's fit served all the rest, and a cached failure
  blinded the block until midnight.
- **A failure TTL was tried on that cache and REVERTED.** It reintroduced the
  per-ticker refit for exactly the expensive case the cache exists to prevent, and
  `test_hmm_failures_are_cached_too` was right to fail. *When a test contradicts
  your change, establish which is wrong before touching either.* Cycle-keying
  already solves the sticky-failure problem.
- **`assert block` tests the environment, not the property.** The composition test
  first asserted the block was non-empty — which passes or fails on whether a DB
  is reachable. It now compares the block *with* a hung component against the same
  block *without* one. Baseline comparison, not truthiness.
- **Patch at the SOURCE module.** `context_block` and `data_report` both import
  their collaborators **inside the function body**, so patching the importer's
  namespace silently misses and the test passes for the wrong reason. Bit me twice.
- **`app/data/` is gitignored but `app/data/sp500_price_collector.py` is TRACKED**
  and shipped (`COPY app/`). `git add` refuses it; `git add -f` is correct here.
  A fix to that file will otherwise never deploy.
- **The permissive default was the root cause, twice.** `board_reasoned` as the
  fallback for unstamped artifacts meant the Triage/JA hardcoded `HOLD@0` writers
  — where no agent ran at all — were scored as real board opinions. Fail-closed
  now (`unattributed`).
- **"Degraded" and "not scoreable" are DIFFERENT questions.** A deliberate skip is
  a correct outcome, not a pipeline failure. Conflating them relabels healthy
  skips across the dashboard, memory store and `policy_action`.

---

## Verification

- **1244 passed**, 2 failed, 15 skipped. Both failures are pre-existing
  (`test_parameter_tools.py`, `test_tool_whitelists.py`) — **verified by
  `git stash`**, they fail identically with every change removed.
- +64 tests over the 1180 baseline, **zero new failures**.
- DB backed up before deploy: `/tmp/trading_bot-20260725-173614.dump` (955M) on
  the NAS, with row counts recorded (shared_desk 1198, trade_results 605,
  technicals 1293339, price_history 15137444).
- No cycle was in flight at deploy time (`/api/v1/bot/cycle_running` → false).

---

## ⚠ One live behavior change

`portfolio_tools.py:147` used a bare `settings.BOT_ID`, so `get_portfolio_state`
reported **`lazy-trader-v4`'s empty book** instead of the active bot's. Every
agent asking for portfolio state was told it held nothing. Fixing it is the point,
but **agents will now see real holdings where they previously saw none** — watch
the first cycle's sizing and held-flag behaviour.

---

## Open / next

1. **Run the Tier 2 agentic tests against the deployed container**
   (`ADVERSARIAL_AGENTIC=1`). They are written and gated but **have not been run** —
   they need prism/vLLM. This is the highest-value next step because they test the
   *harness*, not just this code, including whether agents actually call tools.
2. **Force one degraded cycle with `trade=False`** to exercise the sentinel path
   end to end on real data. Still never done; it is what let the P0 ship.
3. **Calibrate `_COMPONENT_BUDGET_SEC` (45.0)** against real container timings —
   it is currently a reasoned guess sitting under the 60s outer timeout.
4. **Check `v3_guardrail_firings` is non-empty** after a live cycle, and that
   `triage_tier` separates delta from full-panel.
5. **`v3_agent_telemetry` has the identical missing-migration problem** —
   lazily created, absent from both `migrations.py` and `schema_pg.sql`.
6. **The fifth bot_id resolver** in `bot_manager.py` has different semantics (a
   `"default"` sentinel). Merging it is an ownership decision, not a rename.
7. ✅ **The sp500 technicals refresh** — flagged here as "unmeasured at real
   scale", and it bit within minutes. Throttled in `4517ba1` and **verified on
   the redeploy: 11 consecutive healthy samples across the same window that
   previously failed 3 checks, `FailingStreak=0`, and
   `507/507 tickers (0 failed, 0 deferred)`.** The `0 deferred` is the number to
   watch on the next daily run: a non-zero tail means the 240s ceiling is
   binding and wants raising.
