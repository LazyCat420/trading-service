# HANDOFF — The self-healing loop now grades patches instead of debating them (2026-07-27)

Shipped `cd7f606`, deployed to `synology` 2026-07-27 09:14Z. Previous wave's
handoff archived to [`docs/HANDOFF_report_audit_2026-07-25.md`](docs/HANDOFF_report_audit_2026-07-25.md).

---

## What is live right now

The hourly self-healing watchdog no longer proposes patches. It resolves a cycle
failure to a symbol, writes one row to `evolution_repair_queue`, and stops.
Repair happens on a **host checkout** via `scripts/evo_runner.py`, which
proposes on both vLLM boxes, applies each candidate in a throwaway git worktree,
and **scores it by running the tests**. A candidate that fixes a red test
without regressing the baseline gets a branch. Nothing merges, nothing deploys.

The split is forced, not stylistic: the trading-service image has **no `.git`,
no `git` binary, and no `pytest`**. Nothing inside the container can verify a
patch, which is why the old loop could not either.

### Why the old loop was replaced

It was a proposer/critic/judge council modelled on
[CORAL](https://github.com/Human-Agent-Society/CORAL) that had dropped the part
of CORAL that carries the weight — a grader that scores every candidate.
Measured over its entire history (`pending_evolution_fixes`, 96 rows):

| | |
|---|---|
| debates that produced a usable fix | **0 of 57** |
| rejected for literal mid-statement truncation | 33 |
| rejected on prism 500s (dead `vllm_client.py` path, May) | 14 |
| repair targets larger than the 4,000-char view the proposer got | **27 of 30** |
| targets too large to re-emit in a 4096-token completion at all | **11 of 30** |
| cost per proposer call, measured | ~20 min, 47k input tokens |

The judge saw `proposed_fix[:3000]` and *never saw the original file*, so it
could not detect deletion: two of three judges scored one candidate 90 and 100
for "issue resolution" while it deleted nine functions including
`yfinance_collector.collect_all`.

### The new module — `app/cognition/evolution/coral/`

| file | what it does |
|---|---|
| `grader.py` | `ScoreBundle` from pytest: compiles → public API preserved → repro passes → suite no worse than a **captured baseline** |
| `repro.py` | the negative control; a test is discarded unless it **fails on unmodified HEAD** |
| `patcher.py` | unified diffs through a ladder of `git apply` strategies |
| `worktree.py` | one disposable worktree per candidate |
| `loop.py` | both boxes propose in parallel, ranked by score. **No judge.** |
| `attempts.py` | commit-keyed attempts, leaderboard, plateau stop |

Score ladder (`types.py`): `0.00` did not apply / does not compile · `0.25`
repro still fails · `0.60` repro passes but something regressed or a public
symbol was deleted · `1.00` green.

### Verified end to end

Run against a genuinely red test
(`test_parameter_tools.py::test_whitelists_grant_write_to_pm_and_board_only`):
**both islands independently scored 1.00** on a two-file patch —
`1448 passed / 1 failed` against a baseline of 2 failures. Branch
`evo/fundamental_analyst-80e6d46c` exists **locally, unpushed** (see Open items).

---

## How to run it

```bash
python scripts/evo_runner.py --list          # what the container queued
python scripts/evo_runner.py --once          # drain one job
python scripts/evo_runner.py --drain         # until empty
python scripts/evo_runner.py --baseline      # re-characterise the suite
python scripts/evo_runner.py --leaderboard app/collectors/yfinance_collector.py

# ad-hoc, skipping the queue
python scripts/evo_runner.py --path-only --path app/v3/agents/fundamental_analyst.py \
  --context app/v3/agents/quant_analyst.py \
  --repro-test "tests/unit/test_x.py::test_y" --no-push
```

---

## Open items

1. **`evo/fundamental_analyst-80e6d46c` is unpushed and needs a human call.**
   The patch adds `get_parameters` back to the fundamental and quant analyst
   whitelists. It goes green — but it *reverts a deliberate removal*: both files
   carried a comment saying `get_parameters` was dropped on 2026-07-25 for zero
   calls in 60 days. So either that removal was wrong, or
   `test_whitelists_grant_write_to_pm_and_board_only` is a stale expectation and
   the **test** should change. The loop cannot decide this and correctly refused
   to touch `tests/` (see Gotchas). Decide, then push or `git branch -D` it.

2. **`tests/unit/test_tool_whitelists.py::test_quant_analyst_has_calculator_tools`**
   is the other pre-existing baseline failure. Same shape, not investigated.

3. **`debate.py` (926 lines) and `deployer.py` are now unreferenced** by the
   watchdog but still on disk, and the Auto-Research panel still reads
   `pending_evolution_fixes`. That table is a graveyard: 88 of its 96 rows are
   from May, and every `scraper_issue` row plus the `FAILED_REQUIRES_HUMAN`
   status was written by code that no longer exists in the repo. Either point
   the panel at `evolution_attempts` or add a date window — as it stands the UI
   presents May fossils as a live queue.

4. **The queue has no drain schedule.** `evo_runner.py` is manual. If you want it
   automatic it needs a cron/systemd unit *on the workstation*, not the NAS.

---

## Gotchas

- **The grader compares against a captured baseline, not against all-green.**
  This repo has two known failing tests; a rule demanding zero failures would
  reject every patch forever. Baselines are cached per HEAD sha under
  `.evo-worktrees/baselines/` — `--baseline` refreshes.

- **`tests/` is deny-listed in `repair_scope.py` on purpose.** A fixer that can
  edit the test can pass by rewriting the assertion. This is why item 1 above is
  a human decision and not something the loop can resolve.

- **No reproduction test means nothing can score above 0.25**, by design. A loop
  graded only on "the suite still passes" rewards an empty diff.

- **Qwen3-class models bill reasoning to the output budget.** Before
  `vllm_direct.py` disabled thinking for this task, every jetson proposal spent
  all 4096 tokens inside `<think>` and returned `content: ""` — scored 0.00 for
  "empty response" when the model had reasoned fine and never got to write.

- **`llm.chat(endpoint_override=...)` in `prism_agent_caller.py` is still
  silently dropped** (so are `history`, `tools`, `images`). Anything that thinks
  it is choosing a box through `llm.chat` is not. The repair loop sidesteps this
  by calling `/v1/chat/completions` directly; other callers have not been
  audited.

- **Unmapped `evo_*` / unknown agent names resolve to
  `CUSTOM_SYSTEM_JANITOR_AGENT`** via `resolve_agent_id`'s fallback. That is how
  three "roles" became one persona. Check `resolve_agent_id(name)` before
  assuming a new agent name routes anywhere.

- **A bug that spans two files needs `--context`.** A single-file diff can never
  make a control pass that requires both; the loop will honestly grind at 0.25.
  It parks a target after 6 graded attempts (`PLATEAU_ATTEMPTS`).

---

## Where the reasoning lives

- CORAL upstream: https://github.com/Human-Agent-Society/CORAL — the parts taken
  (grader, worktrees, attempts leaderboard, pivot-on-plateau, islands) and the
  part deliberately not taken (agent runtimes) are documented in
  `app/cognition/evolution/coral/__init__.py`.
- Every module docstring in `coral/` states what it replaces and the measurement
  that justified replacing it.
- `tests/unit/test_coral_repair.py` — 33 tests, the score ladder and the two
  checks a syntax check cannot make (empty diff, deleted public symbol).
