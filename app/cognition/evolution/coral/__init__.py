"""CORAL-style repair loop: measured fitness instead of a judge's opinion.

Ported from the ideas in https://github.com/Human-Agent-Society/CORAL, adapted
to run entirely on the local vLLM boxes.

What was taken:

* **A grader scores every candidate.** CORAL's only interface contract is
  ``grade(codebase_path, tasks) -> ScoreBundle``. Fitness is *measured*, never
  voted on. Here that is pytest: a generated reproduction test that must fail
  before the patch, plus the existing suite, compared against a captured
  baseline rather than against "all green".
* **Worktree isolation.** Each candidate is applied and graded in its own
  ``git worktree``; nothing touches the checkout until a patch has earned it.
* **Attempts as durable shared state.** Every candidate is recorded with its
  score, keyed by the commit it produced, and the next round of proposers reads
  the leaderboard — CORAL's ``.coral/public/attempts`` as a table.
* **Pivot on plateau.** Consecutive attempts that fail to beat the baseline stop
  the target rather than grinding, mirroring CORAL's heartbeat.
* **Islands.** The two vLLM boxes run different model families (Qwen on jetson,
  Gemma on dgx_spark), so a round genuinely samples two distributions.

What was deliberately NOT taken: the agent runtimes (Claude Code, Codex, …).
They need tool-calling and network egress this loop does not have, and the whole
point of the rewrite is that the *grader* carries the quality, not the author.

What this replaces: ``debate.py``'s proposer/critic/judge council, which asked a
4096-token completion to re-emit whole files it had only seen 4,000 chars of,
and then let an LLM judge score the result without ever showing it the original.
"""
