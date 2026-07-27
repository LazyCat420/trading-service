#!/usr/bin/env python3
"""Host-side runner for the CORAL repair loop.

Runs on a machine with a git checkout, git, and pytest — which is *not* the
trading-service container: its image ships source without .git, has no git
binary, and no pytest, so nothing in the container can grade a patch. That is
why the container only enqueues, and this drains.

    # what is waiting
    python scripts/evo_runner.py --list

    # take one job, propose on both boxes, grade, push a branch if green
    python scripts/evo_runner.py --once

    # keep going until the queue is empty
    python scripts/evo_runner.py --drain

    # try a specific symbol without going through the queue
    python scripts/evo_runner.py --symbol collect_all --path app/collectors/yfinance_collector.py

    # grade only, never push
    python scripts/evo_runner.py --once --no-push
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.cognition.evolution.coral import attempts as store          # noqa: E402
from app.cognition.evolution.coral.grader import capture_baseline    # noqa: E402
from app.cognition.evolution.coral.loop import RepairFailed, run_repair  # noqa: E402
from app.cognition.evolution.coral.types import RepairJob            # noqa: E402
from app.cognition.evolution.coral.worktree import (                 # noqa: E402
    NotAGitCheckout, assert_git_available,
)

logger = logging.getLogger("evo_runner")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _print_result(result: dict) -> None:
    print()
    print(f"  target        {result['target']}")
    print(f"  attempts      {result['attempts']} over {result['rounds']} round(s)")
    print(f"  best score    {result['best_score']:.2f}")
    print(f"  verdict       {result['best_summary']}")
    if result.get("repro_test"):
        print(f"  repro test    {result['repro_test']}")
    elif result.get("repro_note"):
        print(f"  repro test    NONE — {result['repro_note']}")
        print("                (capped at 0.25: nothing can be pushed without a control)")
    if result.get("branch"):
        print(f"  branch        {result['branch']}")
        print(f"  open a PR     {result['compare_url']}")
    print()


async def _run_job(job: RepairJob, args: argparse.Namespace) -> bool:
    logger.info(
        "[RUNNER] job %s — %s::%s", job.id[:8], job.target_path, job.target_symbol
    )
    try:
        result = await run_repair(job, rounds=args.rounds, push=not args.no_push)
    except RepairFailed as e:
        logger.warning("[RUNNER] job %s not repaired: %s", job.id[:8], e)
        store.finish_job(job.id, "skipped", str(e))
        return False
    except Exception as e:  # noqa: BLE001 — a crashed job must not stop the drain
        logger.exception("[RUNNER] job %s crashed", job.id[:8])
        store.finish_job(job.id, "failed", str(e))
        return False

    _print_result(result)
    store.finish_job(
        job.id,
        "done" if result["best_score"] >= 1.0 else "failed",
        result["best_summary"],
    )
    return result["best_score"] >= 1.0


async def main_async(args: argparse.Namespace) -> int:
    try:
        head = assert_git_available()
    except NotAGitCheckout as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    logger.info("[RUNNER] checkout %s at %s", PROJECT_ROOT, head[:8])

    if args.list:
        jobs = store.open_jobs()
        if not jobs:
            print("queue is empty")
            return 0
        for j in jobs:
            print(f"{j.id[:8]}  {j.status:8}  attempts={j.attempts}  "
                  f"{j.target_path}::{j.target_symbol}  {j.error_message[:60]}")
        return 0

    if args.leaderboard:
        rows = store.leaderboard(args.leaderboard)
        if not rows:
            print(f"no attempts recorded for {args.leaderboard}")
            return 0
        for r in rows:
            print(f"{r['score']:.2f}  {r['island']:<10} {r['created_at'][:19]}  "
                  f"{(r['bundle'] or {}).get('detail', '')[:80]}")
        return 0

    if args.baseline:
        b = capture_baseline(refresh=True)
        print(json.dumps({k: v for k, v in b.items() if k != "failures"}, indent=2))
        print(f"known failures ({len(b['failures'])}):")
        for f in b["failures"]:
            print(f"  {f}")
        return 0

    if args.symbol or args.path_only:
        if args.path_only and not args.path:
            print("error: --path-only needs --path", file=sys.stderr)
            return 2
        job = RepairJob(
            id=str(uuid.uuid4()),
            cycle_id="manual",
            error_message=args.error
            or f"manual repair request for {args.symbol or args.path}",
            traceback_text=(
                Path(args.traceback_file).read_text() if args.traceback_file else ""
            ),
            target_path=args.path,
            target_symbol=args.symbol,
            context_paths=[c.strip() for c in (args.context or "").split(",") if c.strip()],
            repro_test=args.repro_test,
        )
        return 0 if await _run_job(job, args) else 1

    ran = 0
    while True:
        job = store.claim_next_job()
        if job is None:
            if ran == 0:
                print("queue is empty")
            break
        await _run_job(job, args)
        ran += 1
        if not args.drain:
            break
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="claim and run a single queued job (default)")
    mode.add_argument("--drain", action="store_true",
                      help="keep claiming jobs until the queue is empty")
    mode.add_argument("--list", action="store_true", help="show the open queue")
    mode.add_argument("--leaderboard", metavar="PATH",
                      help="show graded attempts for a repo-relative file")
    mode.add_argument("--baseline", action="store_true",
                      help="re-capture the test baseline for HEAD and print it")
    mode.add_argument("--symbol", help="repair this symbol directly, skipping the queue")

    p.add_argument("--path", help="file that defines --symbol (disambiguates)")
    p.add_argument("--error", help="error message for a --symbol run")
    p.add_argument("--traceback-file", help="file containing the traceback for --symbol")
    p.add_argument("--repro-test", metavar="NODEID",
                   help="an existing pytest node id that already fails because of "
                        "this bug; used as the control instead of generating one")
    p.add_argument("--context", metavar="PATHS",
                   help="comma-separated extra files to show the proposer, for a "
                        "bug that spans more than one module")
    p.add_argument("--path-only", action="store_true",
                   help="target the whole module at --path (no symbol)")
    p.add_argument("--rounds", type=int, default=2,
                   help="proposal rounds per job (default 2)")
    p.add_argument("--no-push", action="store_true",
                   help="grade and branch locally, but do not push")
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args()
    _setup_logging(args.verbose)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
