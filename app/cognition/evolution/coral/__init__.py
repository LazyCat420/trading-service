"""Patch grading: measured fitness instead of an opinion.

What remains of a CORAL-style repair loop, after the loop was removed on
2026-07-31 and the grader was kept.

The proposing is gone. It ranked LLM-written patches by "the test suite goes
green", produced exactly two top-scored patches in its life, and both were
wrong in the same way: they re-added `get_parameters` to the analyst
whitelists — inserting the line directly above the comment explaining why a
human had deliberately removed it — because a stale test still demanded it.
Green is only the right target when the tests are right, and nothing in the
loop could ask that question. It cost ~4,800 lines to learn.

The grading is the part that was always good, and it is kept whole, driven by
`scripts/grade_patch.py` against a patch a human wrote:

* **Fitness is measured, never voted on.** pytest decides: a reproduction test
  that must FAIL before the patch, plus the existing suite compared against a
  captured baseline rather than against "all green" — this repo carries
  pre-existing failures, and a grader demanding all-green rejects everything
  forever.
* **Worktree isolation.** A patch is applied and graded in its own throwaway
  ``git worktree``, so a destructive edit destroys a copy. This is what let the
  old size-ratio heuristic be deleted.
* **Deleted public symbols are a regression** even when every test passes —
  that is how the old loop's best-scoring proposal passed review while removing
  nine functions, including a collector's entrypoint.

`attempts.py` is now a failure log rather than a work queue: the watchdog still
records which cycle failures were in patchable scope, and nothing drains it.
"""
