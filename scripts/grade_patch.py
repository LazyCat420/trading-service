#!/usr/bin/env python3
"""Grade a patch honestly. You choose the patch; this decides whether it works.

This replaces CORAL's autonomous repair loop, and the reason is worth keeping
written down. That loop proposed patches with an LLM and ranked them by "the
test suite goes green". It produced exactly two top-scored patches in its life,
and both were wrong in the same way: they re-added `get_parameters` to the
analyst whitelists — inserting the line directly above the comment explaining
why a human had deliberately removed it on 2026-07-25 — because a stale test
still demanded it. Green was the wrong target, and nothing in the loop could
ask "is this test still right?".

So the grading survives and the proposing does not. A human decides what to
change and vouches for the test; this measures whether the change does what it
claims without breaking anything else. Every guarantee the loop had is kept:

  * the patch is graded in a THROWAWAY worktree, so a destructive edit destroys
    a copy rather than your checkout;
  * the reproduction test must FAIL on unmodified HEAD before the patch may
    claim credit for fixing it — a test that passes without the patch proves
    nothing;
  * regressions are measured against a CAPTURED baseline, not against zero
    failures, because this repo carries pre-existing failures and a grader that
    demands all-green rejects every patch forever;
  * deleted public symbols are a regression even when every test passes.

Usage:
    scripts/grade_patch.py <branch-or-ref>
    scripts/grade_patch.py <branch> --repro tests/unit/test_x.py::test_y
    scripts/grade_patch.py <branch> --repro tests/unit/test_x.py --suite tests/unit

Exit codes:  0 = scored 1.0    1 = scored below 1.0    2 = could not grade
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.cognition.evolution.coral.grader import (       # noqa: E402
    DEFAULT_SUITE, REPRO_TIMEOUT_S, BaselineUnavailable, _run_pytest,
    capture_baseline, grade,
)
from app.cognition.evolution.coral.worktree import (     # noqa: E402
    NotAGitCheckout, assert_git_available, attempt_worktree, git,
)

logger = logging.getLogger("grade_patch")


def _changed_files(base: str, ref: str) -> list[str]:
    out = git("diff", "--name-only", f"{base}...{ref}")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _repro_file(repro: str) -> str:
    """`tests/x.py::test_y` -> `tests/x.py`. pytest ids may carry ::params."""
    return repro.split("::", 1)[0]


def negative_control(repro: str, ref: str, base: str) -> tuple[bool, str]:
    """Confirm the repro FAILS at ``base``. Returns (ok, explanation).

    The test is taken from the patch when the patch adds or edits it, which is
    the normal shape — a fix and its regression test arrive together. Checking
    it out onto an otherwise-unpatched tree is what makes this a real control:
    the test is present, the fix is not, and it must fail.
    """
    rel = _repro_file(repro)
    with attempt_worktree("negctl", ref=base) as wt:
        in_patch = rel in _changed_files(base, ref)
        if in_patch:
            try:
                git("checkout", ref, "--", rel, cwd=wt)
            except RuntimeError as e:
                return False, f"could not take {rel} from {ref}: {e}"
        elif not (wt / rel).exists():
            return False, (
                f"{rel} exists neither at {base} nor in {ref} — there is no "
                f"test to run"
            )

        rc, output = _run_pytest(wt, [repro], REPRO_TIMEOUT_S, tb="line")
        if rc == 5:
            return False, f"{repro} collected no tests at {base}"
        if rc == 0:
            return False, (
                f"{repro} PASSES at {base}, without the patch. It does not "
                f"reproduce anything, so this patch cannot be credited with "
                f"fixing it. Write a test that fails first."
            )
        tail = "\n".join(output.strip().splitlines()[-3:])
        return True, f"fails at {base} as required\n    {tail}"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Grade a patch in a throwaway worktree.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("ref", help="branch, tag or sha holding the patch")
    p.add_argument("--repro", default=None,
                   help="pytest id of the test that must fail without the patch")
    p.add_argument("--suite", default=",".join(DEFAULT_SUITE),
                   help=f"comma-separated suite (default {','.join(DEFAULT_SUITE)})")
    p.add_argument("--base", default="HEAD",
                   help="what to grade against (default HEAD)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        assert_git_available()
    except NotAGitCheckout as e:
        print(f"cannot grade here: {e}", file=sys.stderr)
        return 2

    suite = tuple(s for s in args.suite.split(",") if s)
    try:
        base_sha = git("rev-parse", args.base)
        ref_sha = git("rev-parse", args.ref)
    except RuntimeError as e:
        print(f"cannot resolve refs: {e}", file=sys.stderr)
        return 2

    if base_sha == ref_sha:
        print(f"{args.ref} and {args.base} are the same commit — nothing to grade.",
              file=sys.stderr)
        return 2

    changed = _changed_files(args.base, args.ref)
    if not changed:
        print(f"{args.ref} changes no files against {args.base}.", file=sys.stderr)
        return 2

    print(f"grading {args.ref} ({ref_sha[:8]}) against {args.base} ({base_sha[:8]})")
    print(f"  {len(changed)} file(s): {', '.join(changed[:6])}"
          f"{' …' if len(changed) > 6 else ''}")

    # ── negative control ─────────────────────────────────────────────
    if args.repro:
        ok, why = negative_control(args.repro, args.ref, args.base)
        print(f"  negative control: {why}")
        if not ok:
            print("\nREFUSED — the control failed, so the grade would be meaningless.")
            return 2
    else:
        print("  negative control: SKIPPED (no --repro) — cannot score above 0.25")

    # ── baseline + grade ─────────────────────────────────────────────
    print(f"  baseline: capturing {'/'.join(suite)} at {args.base} "
          f"(cached per commit, ~3.5 min the first time)")
    try:
        baseline = capture_baseline(suite=suite, ref=base_sha)
    except BaselineUnavailable as e:
        print(f"\nREFUSED — no usable baseline, so nothing can be scored: {e}",
              file=sys.stderr)
        return 2
    print(f"  baseline: {len(baseline.get('failures') or [])} pre-existing failure(s), "
          f"{baseline.get('passed')} passing")

    with attempt_worktree("grade", ref=args.ref) as wt:
        bundle = grade(
            wt,
            changed_files=changed,
            repro_test=args.repro,
            baseline=baseline,
            suite=suite,
            base_ref=base_sha,
        )

    print("\n" + "=" * 62)
    print(f"  SCORE {bundle.score:.2f}   ({bundle.duration_s}s)")
    print("=" * 62)
    print(f"  compiles ......... {bundle.compiles}")
    print(f"  repro passes ..... {bundle.repro_passed}"
          f"{'' if args.repro else '  (no repro supplied)'}")
    print(f"  suite ............ {bundle.tests_passed} passed, "
          f"{bundle.tests_failed} failed "
          f"(baseline had {bundle.baseline_failed})")
    if bundle.new_failures:
        print(f"  NEW FAILURES ..... {len(bundle.new_failures)}")
        for f in bundle.new_failures[:10]:
            print(f"      {f}")
    if bundle.fixed_failures:
        print(f"  fixed ............ {len(bundle.fixed_failures)}")
        for f in bundle.fixed_failures[:10]:
            print(f"      {f}")
    if bundle.api_removed:
        print(f"  PUBLIC SYMBOLS DELETED: {', '.join(bundle.api_removed)}")
    if bundle.detail:
        print(f"\n  {bundle.detail.strip()}")

    # 1.0 is the only score that means "this is ready". Everything else is a
    # non-zero exit so a caller cannot mistake 0.60 for success.
    return 0 if bundle.score >= 1.0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
