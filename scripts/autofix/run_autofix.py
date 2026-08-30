#!/usr/bin/env python3
"""Drive an external coding agent (omp) at one defect, and grade what it wrote.

This is the proposing half that was deleted on 2026-07-31, rebuilt against a
grader that has since been fault-injected and repaired (2026-08-09). It exists
because the deletion was right about the *loop* and wrong about nothing else:
grading a patch is valuable, proposing one is cheap, and the failure was that
nothing could ask whether the test being satisfied was still the right test.

Three things are different this time.

**It is fail-closed on its input.** The old loop would repair anything the
watchdog handed it. Measured on 2026-08-09, that feed contains no code defects
at all: `evolution_repair_queue` held 4 rows in two weeks, every one with an
EMPTY traceback, because the watchdog reads `pipeline_state.error` (a string)
rather than a stack trace. The 82 rows in `execution_errors` that do carry a
traceback are ~100% infrastructure — a vLLM box that is down, Prism 500s, a job
cancelled on timeout. Handing "yfinance returned no data for BKE" to a coding
agent does not produce a fix; it produces an invention. So a job with no
traceback and no reproduction test is REFUSED here, by name, rather than
becoming this loop's first confident mistake.

**The test is written before the fix, by a session that cannot see a fix.**
A single agent asked for "a fix and a test proving it" writes the pair that is
easiest to satisfy together, which is the tautology the negative control was
supposed to catch and does not: a test that fails at base for a trivial reason
and passes with a trivial edit clears every mechanical gate. Here phase A may
touch only the test file, phase B may not touch it at all, and the diff is
checked against both rules rather than trusted.

**Nothing merges and nothing deploys.** A score of 1.0 pushes a branch and
records a row whose `human_verdict` is `pending`. The one question the grader
cannot ask — is this test right? — is the one left for a person.

Usage:
    scripts/autofix/run_autofix.py --job <queue-id>
    scripts/autofix/run_autofix.py --task "<what is broken>" --target app/v3/x.py
    scripts/autofix/run_autofix.py --job <id> --repro tests/unit/test_x.py::test_y
    scripts/autofix/run_autofix.py --list

Exit codes:  0 = pushed a graded 1.0 branch   1 = graded below 1.0   2 = refused
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.cognition.evolution.coral.grader import (          # noqa: E402
    DEFAULT_SUITE, REPRO_TIMEOUT_S, BaselineUnavailable, _run_pytest,
    capture_baseline, grade,
)
from app.cognition.evolution.coral.worktree import (        # noqa: E402
    NotAGitCheckout, assert_git_available, attempt_worktree, commit_and_label,
    compare_url, git, push_branch,
)
from app.cognition.evolution.repair_scope import (              # noqa: E402
    ALLOWED_PREFIXES, is_patchable,
)
from app.db import mongo_query, mongo_store                                   # noqa: E402

logger = logging.getLogger("run_autofix")

# The local vLLM box, through the shim that fixes the thinking-flag spelling.
# Overridable because the box that is up changes; see the vllm-shim notes.
DEFAULT_MODEL = os.environ.get("AUTOFIX_MODEL", "gold-spark/deepseek-v4-flash-0731")

OMP_TIMEOUT_S = int(os.environ.get("AUTOFIX_OMP_TIMEOUT_S", "900"))

# A day's worth of pushed branches. The cap is low on purpose: this loop's
# failure mode is volume of plausible-looking patches, not scarcity.
MAX_PUSHES_PER_DAY = 5
MAX_ATTEMPTS_PER_JOB = 3


class Refused(RuntimeError):
    """The run cannot be made meaningful, so it does not start."""


# ── Input ───────────────────────────────────────────────────────────────────


def load_job(job_id: str) -> dict:
    row = mongo_query.find_row(
        "evolution_repair_queue", {"id": job_id},
        ["id", "cycle_id", "error_message", "traceback_text", "target_path",
         "target_symbol", "repro_test", "status", "attempts"],
    )
    if not row:
        raise Refused(f"no queue row with id {job_id}")
    keys = ("id", "cycle_id", "error_message", "traceback_text", "target_path",
            "target_symbol", "repro_test", "status", "attempts")
    return dict(zip(keys, row))


def assert_job_is_repairable(job: dict) -> None:
    """Refuse a job that describes an event rather than a defect.

    This is the guard the 2026-07-31 deletion was really about. A queue row
    whose traceback is empty and whose repro test is unset carries no evidence
    that any code is wrong — the watchdog logged an operational message and
    resolved it to a file through a fallback name map. There is nothing for an
    agent to reproduce, so anything it writes is invention that the grader will
    then certify because the agent also wrote the test.
    """
    if (job.get("attempts") or 0) >= MAX_ATTEMPTS_PER_JOB:
        raise Refused(
            f"job {job['id'][:8]} has already been attempted "
            f"{job['attempts']} times (cap {MAX_ATTEMPTS_PER_JOB})"
        )

    traceback_text = (job.get("traceback_text") or "").strip()
    repro = (job.get("repro_test") or "").strip()
    if not traceback_text and not repro:
        raise Refused(
            f"job {job['id'][:8]} has an EMPTY traceback and no repro test.\n"
            f"  error_message: {(job.get('error_message') or '')[:120]!r}\n"
            f"  This records an operational event, not a code defect — the\n"
            f"  watchdog reads pipeline_state.error, which is a message string,\n"
            f"  and resolved it to {job.get('target_path')} through the fallback\n"
            f"  name map rather than from a stack frame. There is nothing here\n"
            f"  to reproduce. Fix the feed, or pass --task/--repro explicitly."
        )

    target = job.get("target_path") or ""
    if target:
        allowed, reason = is_patchable(target)
        if not allowed:
            raise Refused(f"{target} is outside the repair scope: {reason}")


# ── The coding agent ────────────────────────────────────────────────────────


def run_omp(worktree: Path, prompt: str, *, model: str, label: str) -> str:
    """One non-interactive omp session in ``worktree``. Returns its final text.

    Print mode is deliberate: each phase gets a session that cannot see the
    other's reasoning, only the tree it left behind.
    """
    logger.info("[AUTOFIX] omp %s (model=%s, cwd=%s)", label, model, worktree)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["omp", "-p", "--model", model, "--auto-approve", "--no-session",
             "--cwd", str(worktree), prompt],
            capture_output=True, text=True, timeout=OMP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise Refused(f"omp {label} exceeded {OMP_TIMEOUT_S}s")
    except FileNotFoundError:
        raise Refused("omp is not on PATH — install @oh-my-pi/pi-coding-agent")
    elapsed = round(time.monotonic() - started, 1)
    if proc.returncode != 0:
        raise Refused(
            f"omp {label} exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '')[-400:]}"
        )
    logger.info("[AUTOFIX] omp %s finished in %.0fs", label, elapsed)
    return (proc.stdout or "").strip()


def dirty_files(worktree: Path) -> list[str]:
    """Paths changed in the worktree, staged or not, including new files.

    Deliberately NOT ``git status --porcelain``: that format encodes the
    status in two fixed columns, and ``git()`` strips its output, which eats
    the leading space of an unstaged line (" M app/x.py") so a column slice
    silently returns "pp/x.py". Caught by the positive control on 2026-08-09,
    where it read as an out-of-scope path and refused a good patch. These two
    commands print bare paths and need no column arithmetic.
    """
    tracked = git("diff", "--name-only", "HEAD", cwd=worktree)
    untracked = git("ls-files", "--others", "--exclude-standard", cwd=worktree)
    seen: list[str] = []
    for blob in (tracked, untracked):
        for line in blob.splitlines():
            path = line.strip()
            if path and path not in seen:
                seen.append(path)
    return sorted(seen)


REPRO_PROMPT = """\
A trading service failed in production. Your ONLY job in this session is to
write a pytest test that REPRODUCES the failure. You must NOT fix anything.

{context}

Rules, all enforced mechanically after you stop:
  * Create exactly ONE new test file at: {repro_path}
  * Change NO other file. Not the source, not another test. A diff touching
    anything else is discarded and this run is recorded as a failure.
  * The test MUST FAIL when run against the current code, for the reason above
    — not because of an import error, a missing fixture, or a typo. A test that
    errors at collection proves nothing and is rejected.
  * Do not write a test that merely asserts the error message string. Reproduce
    the actual defective BEHAVIOUR: call the real code path and assert what it
    should have done.
  * Keep it fast and hermetic. No network, no database, no live services —
    stub at the boundary. The graded suite runs offline.

Run `python -m pytest {repro_path} -x -q` yourself and confirm it fails for the
right reason before you finish. Report the assertion it fails on.
"""

FIX_PROMPT = """\
A trading service has a defect. A reproduction test already exists and
currently FAILS. Your job is to make it pass by fixing the source.

{context}

The failing reproduction test is: {repro_test}

Rules, all enforced mechanically after you stop:
  * Do NOT modify, delete, or weaken ANY test. The test file {repro_path} is
    the evidence your fix works; editing it is how a previous version of this
    loop "succeeded" while reverting a deliberate human decision. A diff that
    touches tests/ is discarded and this run is recorded as a failure.
  * You may only edit source under these prefixes: {allowed}
  * Make the SMALLEST change that fixes the cause. Do not refactor, do not
    reformat, do not rename, do not "improve" adjacent code.
  * Do not delete any public function or class. Deleted public symbols are
    counted as a regression even when every test passes.

Run `python -m pytest {repro_test} -x -q` and confirm it passes. Then briefly
state the root cause and why your change addresses it rather than masking it.
"""


def build_context(job: dict, task: str | None) -> str:
    bits = []
    if task:
        bits.append(f"What is wrong:\n{task}")
    if job.get("error_message"):
        bits.append(f"Error message:\n{job['error_message']}")
    if job.get("target_path"):
        sym = job.get("target_symbol") or ""
        bits.append(f"Suspected location: {job['target_path']}"
                    + (f" (symbol: {sym})" if sym else ""))
    tb = (job.get("traceback_text") or "").strip()
    if tb:
        bits.append(f"Traceback:\n{tb[:4000]}")
    if job.get("cycle_id"):
        bits.append(f"Seen in cycle: {job['cycle_id']}")
    return "\n\n".join(bits)


# ── Ledger ──────────────────────────────────────────────────────────────────


def record_run(*, job_id: str, job: dict, model: str, branch: str | None,
               commit_hash: str | None, url: str | None, score: float | None,
               bundle: dict | None, changed: list[str], wall_clock_s: float,
               pushed: bool, note: str = "") -> str:
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    mongo_store.insert_docs("autofix_runs", [{
        "id": run_id,
        "job_id": job_id,
        "target_path": job.get("target_path"),
        "target_symbol": job.get("target_symbol"),
        "model": model,
        "harness": "omp",
        "branch": branch,
        "commit_hash": commit_hash,
        "compare_url": url,
        "score": score,
        # bundle/changed_files were json.dumps'd for a JSONB column. Mongo
        # stores the structures directly — dumping them here would bury the
        # run's evidence inside a string that no query can reach into.
        "bundle": bundle or None,
        "changed_files": changed,
        "wall_clock_s": wall_clock_s,
        # CASE WHEN pushed THEN CURRENT_TIMESTAMP ELSE NULL END
        "pushed_at": now if pushed else None,
        "human_verdict": "pending" if pushed else "not_pushed",
        "verdict_reason": note[:2000],
        "created_at": now,
    }])
    return run_id


def pushes_today() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    return mongo_store.count_docs("autofix_runs", {"pushed_at": {"$gt": cutoff}})


# ── The run ─────────────────────────────────────────────────────────────────


def run_one(job: dict, *, task: str | None, model: str, suite: tuple[str, ...],
            repro_test: str | None, dry_run: bool) -> int:
    started = time.monotonic()
    job_id = job["id"]
    base_sha = git("rev-parse", "HEAD")
    context = build_context(job, task)

    slug = (job.get("target_symbol") or job.get("target_path") or "autofix")
    slug = "".join(c if c.isalnum() else "_" for c in str(slug))[:40].strip("_")
    repro_path = repro_test.split("::", 1)[0] if repro_test else \
        f"tests/unit/test_autofix_{slug}_{job_id[:8]}.py"

    with attempt_worktree(f"autofix-{job_id[:8]}", ref=base_sha) as wt:
        # ── phase A: reproduce, with no fix in context ──────────────────
        if repro_test:
            logger.info("[AUTOFIX] using supplied repro %s", repro_test)
        else:
            run_omp(wt, REPRO_PROMPT.format(context=context, repro_path=repro_path),
                    model=model, label="phase-A/repro")
            touched = dirty_files(wt)
            if touched != [repro_path]:
                raise Refused(
                    f"phase A was allowed to create only {repro_path}, but the "
                    f"diff touches {touched or 'nothing'}. Discarded."
                )
            repro_test = repro_path

        # The negative control, in the tree the fix has not seen yet.
        rc, output = _run_pytest(wt, [repro_test], REPRO_TIMEOUT_S, tb="short")
        if rc == 5:
            raise Refused(f"{repro_test} collected no tests — it proves nothing")
        if rc == 0:
            raise Refused(
                f"{repro_test} PASSES before any fix. It does not reproduce the "
                f"defect, so nothing can be credited with fixing it."
            )
        # A test that errors at import failed for the wrong reason: it would
        # also "start passing" the moment the import is satisfied, which is not
        # a fix. Distinguishable because pytest reports collection failures as
        # ERROR rather than FAILED.
        if any(ln.startswith("ERROR ") for ln in output.splitlines()):
            raise Refused(
                f"{repro_test} ERRORS at collection rather than failing an "
                f"assertion, so it reproduces nothing:\n"
                + "\n".join(ln for ln in output.splitlines()
                            if ln.startswith("ERROR "))[:400]
            )
        logger.info("[AUTOFIX] negative control OK — %s fails at base", repro_test)

        # Freeze the test so phase B cannot quietly relax it.
        repro_blob = (wt / repro_path).read_text(encoding="utf-8") \
            if (wt / repro_path).exists() else None

        # ── phase B: fix, forbidden from touching the test ──────────────
        run_omp(
            wt,
            FIX_PROMPT.format(
                context=context, repro_test=repro_test, repro_path=repro_path,
                allowed=", ".join(ALLOWED_PREFIXES),
            ),
            model=model, label="phase-B/fix",
        )

        changed = dirty_files(wt)
        source_changed = [f for f in changed if f != repro_path]
        if not source_changed:
            raise Refused("phase B changed no source — there is no patch to grade")

        for rel in source_changed:
            allowed, reason = is_patchable(rel)
            if not allowed:
                raise Refused(
                    f"phase B edited {rel}, which is out of scope ({reason}). "
                    f"Discarded."
                )

        if repro_blob is not None:
            now = (wt / repro_path).read_text(encoding="utf-8") \
                if (wt / repro_path).exists() else None
            if now != repro_blob:
                raise Refused(
                    f"phase B modified the reproduction test {repro_path}. "
                    f"That is the exact move this loop exists to prevent. "
                    f"Discarded."
                )

        # ── grade ───────────────────────────────────────────────────────
        try:
            baseline = capture_baseline(suite=suite, ref=base_sha)
        except BaselineUnavailable as e:
            raise Refused(f"no usable baseline, nothing can be scored: {e}")

        branch = f"fix/autofix-{job_id[:8]}"
        commit_hash = commit_and_label(
            wt,
            f"fix(autofix): {(job.get('error_message') or task or 'repair')[:60]}\n\n"
            f"Proposed by omp ({model}) against queue job {job_id}.\n"
            f"Reproduction test written in a separate session that could not "
            f"see a fix, and verified to fail at {base_sha[:8]} before the fix "
            f"was written.\n\nNOT REVIEWED BY A HUMAN.",
            branch,
            paths=changed,
        )

        bundle = grade(
            wt,
            changed_files=changed,
            repro_test=repro_test,
            baseline=baseline,
            suite=suite,
            base_ref=base_sha,
        )

    wall = round(time.monotonic() - started, 1)
    print("\n" + "=" * 62)
    print(f"  SCORE {bundle.score:.2f}   ({wall}s wall clock)")
    print("=" * 62)
    print(f"  compiles ......... {bundle.compiles}")
    print(f"  repro passes ..... {bundle.repro_passed}")
    print(f"  suite ............ {bundle.tests_passed} passed, "
          f"{bundle.tests_failed} failed (baseline {bundle.baseline_failed})")
    if bundle.new_failures:
        print(f"  NEW FAILURES ..... {len(bundle.new_failures)}")
        for f in bundle.new_failures[:10]:
            print(f"      {f}")
    if bundle.api_removed:
        print(f"  PUBLIC SYMBOLS DELETED: {', '.join(bundle.api_removed)}")
    print(f"  files ............ {', '.join(changed)}")

    pushed = False
    url = None
    if bundle.is_green and not dry_run:
        if pushes_today() >= MAX_PUSHES_PER_DAY:
            print(f"\n  NOT PUSHED — {MAX_PUSHES_PER_DAY} branches already "
                  f"pushed today. Review those first.")
        else:
            url = push_branch(branch)
            pushed = True
            print(f"\n  pushed {branch}")
            print(f"  review: {url}")
    elif bundle.is_green and dry_run:
        url = compare_url(branch)
        print(f"\n  dry run — branch {branch} left local, not pushed")

    run_id = record_run(
        job_id=job_id, job=job, model=model, branch=branch,
        commit_hash=commit_hash, url=url, score=bundle.score,
        bundle=bundle.to_dict(), changed=changed, wall_clock_s=wall,
        pushed=pushed, note=bundle.detail,
    )
    print(f"  recorded autofix_runs.{run_id[:8]} "
          f"(verdict: {'pending human review' if pushed else 'not pushed'})")

    if not bundle.is_green:
        print("\n  Not ready. Nothing was pushed.")
        return 1

    print("\n  A human still has to answer the question the grader cannot:")
    print("  is this test right, and does the fix address the cause?")
    return 0


def list_jobs() -> int:
    from app.cognition.evolution.coral.attempts import open_jobs
    jobs = open_jobs()
    if not jobs:
        print("no open jobs")
        return 0
    print(f"{len(jobs)} open job(s):\n")
    for j in jobs:
        row = mongo_query.find_row(
            "evolution_repair_queue", {"id": j.id},
            ["traceback_text", "repro_test"],
        )
        # coalesce(...,'') -- a missing field comes back as None, and the
        # usable check below does .strip() on it.
        tb, repro = ((row[0] or ""), (row[1] or "")) if row else ("", "")
        usable = bool((tb or "").strip() or (repro or "").strip())
        flag = "OK      " if usable else "REFUSED "
        print(f"  {flag} {j.id[:8]}  {j.target_path}::{j.target_symbol}")
        print(f"            {j.error_message[:88]}")
        if not usable:
            print("            ^ empty traceback, no repro — an event, not a defect")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--job", help="evolution_repair_queue id")
    p.add_argument("--task", help="describe the defect (instead of, or with, --job)")
    p.add_argument("--target", help="suspected file, repo-relative")
    p.add_argument("--repro", help="existing pytest id that already fails")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--suite", default=",".join(DEFAULT_SUITE))
    p.add_argument("--dry-run", action="store_true",
                   help="grade, but never push")
    p.add_argument("--list", action="store_true", help="show open queue jobs")
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
        print(f"cannot run here: {e}", file=sys.stderr)
        return 2

    if args.list:
        return list_jobs()

    if not args.job and not args.task:
        print("need --job or --task (or --list)", file=sys.stderr)
        return 2

    try:
        if args.job:
            job = load_job(args.job)
            if args.repro and not job.get("repro_test"):
                job["repro_test"] = args.repro
            assert_job_is_repairable(job)
        else:
            job = {"id": str(uuid.uuid4()), "cycle_id": "", "error_message": "",
                   "traceback_text": "", "target_path": args.target,
                   "target_symbol": None, "repro_test": args.repro,
                   "status": "adhoc", "attempts": 0}
            if args.target:
                allowed, reason = is_patchable(args.target)
                if not allowed:
                    raise Refused(f"{args.target} is out of scope: {reason}")

        return run_one(
            job,
            task=args.task,
            model=args.model,
            suite=tuple(s for s in args.suite.split(",") if s),
            repro_test=args.repro or (job.get("repro_test") or None),
            dry_run=args.dry_run,
        )
    except Refused as e:
        print(f"\nREFUSED — {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
