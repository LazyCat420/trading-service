"""The grader: the only thing in this loop entitled to say a patch is good.

CORAL's design rests on ``grade(codebase_path, tasks) -> ScoreBundle`` — a
daemon scores every commit, and agents steer on that number. The council this
replaces had no such function. Its fitness was an LLM judge that saw
``proposed_fix[:3000]``, never saw the original file, and had no way to run
anything; two of three judges scored a patch 90 and 100 for "issue resolution"
while it silently deleted nine functions.

Four measurements, in increasing cost, short-circuiting on failure:

1. **compiles** — every changed file parses and byte-compiles.
2. **api_removed** — public defs/classes that existed before and are gone after.
   Free to compute and catches the deletion class of failure outright.
3. **repro** — the generated reproduction test, which was verified to FAIL on
   unmodified HEAD before any patch was written. Without that negative control
   a repro test that passes everywhere measures nothing.
4. **suite** — the existing tests, diffed against a captured baseline rather
   than required to be green, because this repo has a known pre-existing
   failure and an all-green rule would reject every patch forever.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from app.cognition.evolution.coral.types import ScoreBundle
from app.cognition.evolution.coral.worktree import PROJECT_ROOT, git

logger = logging.getLogger(__name__)

# Default graded suite. tests/unit runs in ~50s over ~730 tests and needs no
# database; the integration and regression trees want live services and would
# make the grader's verdict depend on whether the NAS was busy.
DEFAULT_SUITE = ("tests/unit",)

SUITE_TIMEOUT_S = 900
REPRO_TIMEOUT_S = 300

_BASELINE_DIR = PROJECT_ROOT / ".evo-worktrees" / "baselines"


class BaselineUnavailable(RuntimeError):
    """The suite could not be characterised, so nothing can be scored against it."""


_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors)")


def _python() -> str:
    """The interpreter to grade with — the checkout's venv if there is one.

    ``sys.executable`` is wrong when the runner is launched from somewhere else;
    the suite must run against the same dependencies the service ships. The venv
    is untracked, so a linked worktree does not have one — resolve it from the
    main checkout in that case, or 63 test files die on `No module named
    'lazycat'` at collection and the suite signal is destroyed.
    """
    venv = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    try:
        common = Path(git("rev-parse", "--git-common-dir", cwd=PROJECT_ROOT))
        if not common.is_absolute():
            common = PROJECT_ROOT / common
        main_venv = common.resolve().parent / ".venv" / "bin" / "python"
        if main_venv.exists():
            return str(main_venv)
    except RuntimeError:
        pass
    return sys.executable


def _run_pytest(cwd: Path, targets: list[str], timeout: int,
                tb: str = "no") -> tuple[int, str]:
    """Run pytest in ``cwd``. Returns ``(returncode, combined output)``.

    ``-p no:cacheprovider`` keeps a worktree from writing .pytest_cache into a
    directory that is about to be deleted; ``--tb=no -rf`` gives the short
    summary the failure parser reads without the traceback bulk.

    ``tb="short"`` is used for the reproduction test alone, because the actual
    assertion text is the single most useful thing the next round of proposers
    can be told: "it still fails" does not locate the mistake, but
    ``assert 'get_parameters' in [...]`` does.
    """
    env = {
        **os.environ,
        # The container's compose file drops /app from PYTHONPATH, which is why
        # every sandbox run in the old loop died on `No module named 'app'`.
        # Here the worktree root IS the package root, so pin it explicitly
        # rather than relying on cwd injection surviving a -p plugin.
        "PYTHONPATH": str(cwd),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        proc = subprocess.run(
            # -rfE: collection ERRORs must appear in the short summary too, or
            # a suite that dies at import produces counts with no node ids and
            # the baseline diff sees nothing.
            [_python(), "-m", "pytest", *targets,
             "-q", f"--tb={tb}", "-rfE", "-p", "no:cacheprovider"],
            cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        # A patch that hangs the suite is a failure, not a crash of the grader.
        # Returning 1 with no parseable summary scores it as "not green".
        logger.warning("[CORAL-GRADE] pytest %s timed out after %ds", targets, timeout)
        return 1, f"TIMEOUT: pytest exceeded {timeout}s"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _assertion_excerpt(output: str, *, max_chars: int = 1200) -> str:
    """The assertion lines from a pytest --tb=short run.

    Keeping the ``>`` and ``E`` lines gives the next proposer the failing
    expression and the actual value, which is what locates the mistake. The rest
    of the traceback is framework noise.
    """
    keep = [
        ln for ln in output.splitlines()
        if ln.startswith(("E ", ">", "E\t")) or ln.lstrip().startswith("assert ")
    ]
    text = "\n".join(keep) if keep else output
    return text[-max_chars:].strip()


def _parse_failures(output: str) -> tuple[set[str], int, int]:
    """Extract ``(failing node ids, passed, failed)`` from pytest -q output.

    Counts are read from the final summary line only ("1 failed, 732 passed
    ... in 50.57s"). Scanning the whole output double-counts: an interrupted
    collection prints "63 errors during collection" AND "63 errors in 7.57s".
    """
    failures = {m.group(1) for m in _FAILED_RE.finditer(output)}
    passed = failed = 0
    summary_lines = [ln for ln in output.splitlines()
                     if re.search(r"\bin\s+[\d.]+s\b", ln) and _COUNT_RE.search(ln)]
    counts_source = summary_lines[-1] if summary_lines else output
    for count, kind in _COUNT_RE.findall(counts_source):
        if kind == "passed":
            passed = int(count)
        else:
            failed += int(count)
    return failures, passed, failed


# ── Baseline ────────────────────────────────────────────────────────────────


def capture_baseline(*, suite: tuple[str, ...] = DEFAULT_SUITE,
                     refresh: bool = False, ref: str = "HEAD") -> dict:
    """Run the suite at unmodified ``ref`` and cache which tests fail.

    Keyed by the resolved sha: a baseline from a different commit would let a
    patch inherit credit for failures someone else already fixed. ``ref``
    matters when grading a committed branch against an explicit base — the
    baseline must describe the base being graded against, not whatever this
    checkout happens to have checked out.
    """
    head = git("rev-parse", ref)
    _BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _BASELINE_DIR / f"{head}.json"

    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    logger.info("[CORAL-GRADE] capturing baseline for %s (suite=%s)", head[:8], suite)
    started = time.monotonic()
    # In a worktree, never in PROJECT_ROOT: the checkout may have uncommitted
    # work in it, and a baseline taken from a dirty tree would credit or blame
    # candidates for edits nobody in this loop made.
    from app.cognition.evolution.coral.worktree import attempt_worktree

    with attempt_worktree("baseline", ref=head) as wt:
        rc, output = _run_pytest(wt, list(suite), SUITE_TIMEOUT_S)
    failures, passed, failed = _parse_failures(output)

    # A baseline that timed out, collected nothing, or passed nothing is worse
    # than no baseline. With passed == 0 the run is indistinguishable from a
    # broken environment (a missing venv makes half the suite error at import),
    # and a baseline recorded from that state scores garbage as green. Fail
    # loudly.
    if rc == 5 or passed == 0:
        raise BaselineUnavailable(
            f"baseline run produced no usable result (rc={rc}, passed=0). "
            f"Last output: {output[-500:]}"
        )
    baseline = {
        "head": head,
        "suite": list(suite),
        "failures": sorted(failures),
        "passed": passed,
        "failed": failed,
        "duration_s": round(time.monotonic() - started, 1),
    }
    cache.write_text(json.dumps(baseline, indent=2))
    logger.info(
        "[CORAL-GRADE] baseline: %d passed, %d failed in %.0fs",
        passed, failed, baseline["duration_s"],
    )
    return baseline


# ── Static checks ───────────────────────────────────────────────────────────


def _public_symbols(source: str) -> set[str]:
    """Top-level public defs and classes. Underscore names are implementation."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    }


def check_compiles(worktree: Path, files: list[str]) -> tuple[bool, str]:
    for rel in files:
        path = worktree / rel
        if not path.exists():
            return False, f"{rel} does not exist after the patch"
        try:
            ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            return False, f"{rel}:{e.lineno}: {e.msg}"
    return True, ""


def check_api_preserved(worktree: Path, files: list[str],
                        base_ref: str = "HEAD") -> list[str]:
    """Public symbols present at ``base_ref`` and missing after the patch.

    ``base_ref`` must be the pre-patch state. The default of HEAD is only
    correct when the patch sits *uncommitted* in the worktree (the old loop's
    shape). When grading a committed ref the worktree's HEAD IS the patch, and
    comparing it to itself sees no deletion ever — pass the base sha instead.
    """
    removed: list[str] = []
    for rel in files:
        before = git("show", f"{base_ref}:{rel}", cwd=worktree, check=False)
        if not before:
            continue
        after_path = worktree / rel
        after = after_path.read_text(encoding="utf-8", errors="replace") \
            if after_path.exists() else ""
        gone = _public_symbols(before) - _public_symbols(after)
        removed.extend(f"{rel}::{name}" for name in sorted(gone))
    return removed


# ── The grader ──────────────────────────────────────────────────────────────


def grade(
    worktree: Path,
    *,
    changed_files: list[str],
    repro_test: str | None,
    baseline: dict,
    suite: tuple[str, ...] = DEFAULT_SUITE,
    base_ref: str = "HEAD",
) -> ScoreBundle:
    """Score an already-applied patch inside ``worktree``.

    ``repro_test`` is a worktree-relative path to the generated reproduction
    test. When it is None the loop is running without a negative control and the
    bundle can never reach 1.0 — that is deliberate, not a bug: a patch nobody
    can demonstrate fixes anything is not a fix.
    """
    started = time.monotonic()
    bundle = ScoreBundle(applied=True)

    ok, detail = check_compiles(worktree, changed_files)
    bundle.compiles = ok
    if not ok:
        bundle.detail = detail
        bundle.duration_s = round(time.monotonic() - started, 1)
        return bundle

    bundle.api_removed = check_api_preserved(worktree, changed_files,
                                             base_ref=base_ref)

    if repro_test:
        rc, output = _run_pytest(worktree, [repro_test], REPRO_TIMEOUT_S, tb="short")
        bundle.repro_passed = rc == 0
        if rc != 0:
            bundle.detail = (
                "the reproduction test still fails after this patch:\n"
                + _assertion_excerpt(output)
            )
            bundle.duration_s = round(time.monotonic() - started, 1)
            return bundle
    else:
        bundle.detail = "no reproduction test — cannot demonstrate the fix"

    rc, output = _run_pytest(worktree, list(suite), SUITE_TIMEOUT_S)
    failures, passed, failed = _parse_failures(output)
    baseline_failures = set(baseline.get("failures") or [])

    bundle.suite_ran = rc != 5           # 5 == nothing collected
    bundle.tests_passed = passed
    bundle.tests_failed = failed
    bundle.baseline_failed = len(baseline_failures)
    bundle.new_failures = sorted(failures - baseline_failures)
    bundle.fixed_failures = sorted(baseline_failures - failures)

    # A suite that passes nothing where the baseline passed plenty did not run
    # in any meaningful sense — the environment broke (missing venv, dead
    # import), and the node-id diff over an all-error run can come back empty.
    # Scoring that as "no regressions" is how a dead suite grades 1.0.
    baseline_passed = int(baseline.get("passed") or 0)
    if bundle.suite_ran and passed == 0 and baseline_passed > 0:
        bundle.suite_ran = False
        bundle.detail = (
            f"suite collapsed: 0 passed here vs {baseline_passed} at baseline "
            f"— environment failure, not a graded result. Last output:\n"
            + output[-400:]
        )
        bundle.duration_s = round(time.monotonic() - started, 1)
        return bundle

    if not bundle.suite_ran:
        bundle.detail = "suite collected no tests — grading is not meaningful"
    elif not bundle.detail:
        bundle.detail = bundle.summary()

    bundle.duration_s = round(time.monotonic() - started, 1)
    return bundle
