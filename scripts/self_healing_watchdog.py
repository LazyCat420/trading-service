#!/usr/bin/env python3
"""
Self-Healing Watchdog Engine
============================
Detects a trading-cycle failure, resolves it to a symbol, and queues it for
repair. It does not propose a patch, and it has no autonomy level to configure,
because it has nothing to be autonomous *with*.

WHY THE WATCHDOG NO LONGER FIXES ANYTHING
-----------------------------------------
Repair means editing code and proving the edit works. Proving it means running
the tests. The trading-service image has no ``.git``, no ``git`` binary, and no
``pytest`` — so a patch produced here could never be verified by anything
stronger than a syntax check, and that is exactly what used to happen.

It cost, measured: ~20 minutes and 47k input tokens per proposer call, three
calls a round, three rounds, hourly. It returned, measured: 57 rejections out of
57 debates, 33 of them because a 4096-token completion was asked to re-emit a
file it had been shown 4,000 characters of.

The host-side replacement that used to propose patches was removed 2026-07-31.
It ranked LLM-written candidates by "the suite goes green" and produced two
top-scored patches in its life — both wrong the same way, re-adding a tool to
the analyst whitelists directly above the comment explaining why a human had
removed it, because a stale test still demanded it. Green is only the right
target when the tests are right, and no grader can ask that.

So this observes and STOPS. A human writes the fix and grades it with
``scripts/grade_patch.py``, which keeps every guarantee the loop had — throwaway
worktree, a reproduction test that must fail on unmodified HEAD, regressions
measured against a captured baseline, deleted public symbols counted as a
regression.

SCOPE (``repair_scope.is_patchable``) still applies before anything is logged:
only trading-cycle source is repairable. The repair machinery, DB schema,
config, deploy scripts, and tests are off-limits.
"""

import sys
import os
import shutil
import subprocess
import json
import re
import asyncio
import logging
from datetime import datetime, timezone

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.connection import get_db
from app.cognition.evolution.target_map import list_available_targets, resolve_target
from app.cognition.evolution.repair_scope import is_patchable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("self_healing_watchdog")

NAS_HOST = "10.0.0.16"
NAS_PORT = "5188"
NAS_USER = "lazycat"

def run_ssh_command(cmd: str) -> str:
    """Run a command on the NAS over SSH."""
    try:
        res = subprocess.run(
            ["ssh", "-p", NAS_PORT, f"{NAS_USER}@{NAS_HOST}", cmd],
            capture_output=True, text=True, timeout=30
        )
        if res.returncode != 0:
            logger.warning(f"SSH command failed with return code {res.returncode}: {res.stderr}")
        return res.stdout
    except Exception as e:
        logger.error(f"Failed to run SSH command '{cmd}': {e}")
        return ""

def get_active_cycle() -> tuple[str, str, str, str]:
    """Query current pipeline_state to find active cycle ID, status, error, and phase."""
    with get_db() as db:
        db.execute(
            "SELECT cycle_id, status, error, phase FROM pipeline_state WHERE singleton_id = 'current'"
        )
        row = db.fetchone()
        if row:
            return row[0] or "", row[1] or "", row[2] or "", row[3] or ""
    return "", "", "", ""

# Policy gate outcomes are recorded with status='error' in pipeline_events but
# are normal, intended behavior (e.g. a SELL on an unheld position downgraded
# to HOLD_NO_POSITION) — they are NOT crashes and must never trigger healing.
_BENIGN_EVENT_MARKERS = (
    "trade_rejected",
    "SELL_NO_POSITION",
    "HOLD_NO_POSITION",
    "HOLD_NO_SIGNAL",
    "policy_blocked",
)


def _is_benign_policy_event(step: str, detail: str) -> bool:
    text = f"{step or ''} {detail or ''}"
    return any(marker in text for marker in _BENIGN_EVENT_MARKERS)


def get_latest_error_events(cycle_id: str) -> list[dict]:
    """Query recent error events from the database for the given cycle."""
    events = []
    with get_db() as db:
        db.execute(
            """
            SELECT phase, step, detail, timestamp 
            FROM pipeline_events 
            WHERE cycle_id = %s AND status = 'error' 
            ORDER BY timestamp DESC LIMIT 5
            """,
            [cycle_id]
        )
        for row in db.fetchall():
            if _is_benign_policy_event(row[1], row[2]):
                continue
            events.append({
                "phase": row[0],
                "step": row[1],
                "detail": row[2],
                "timestamp": row[3]
            })
    return events

def fetch_nas_cycle_logs(cycle_id: str) -> str:
    """Fetch the JSONL log file for the cycle.

    Inside the container `logs/` IS the NAS volume (/volume1/docker/
    trading-service/logs mounts at /app/logs), so read it locally first —
    the container ships no ssh binary and every SSH attempt just errored.
    SSH remains only as a dev-box fallback when the file isn't local.
    """
    from app.log_manager import log_manager
    local_path = log_manager.CYCLE_DIR / f"{cycle_id}.jsonl"
    try:
        if local_path.exists():
            return local_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Local cycle log read failed for {local_path}: {e}")
    if not shutil.which("ssh"):
        logger.info(f"Cycle log {local_path} not found locally and no ssh binary — skipping log fetch.")
        return ""
    remote_path = f"/volume1/docker/trading-service/logs/cycles/{cycle_id}.jsonl"
    logger.info(f"Fetching remote cycle logs from {remote_path}...")
    return run_ssh_command(f"cat {remote_path}")

def parse_traceback_to_target(tb_text: str) -> dict | None:
    """Parse traceback to identify the failing source file and map it to a target."""
    available = list_available_targets()
    # Find all file paths in the traceback (e.g. File "app/collectors/youtube_collector.py", line 87)
    file_matches = re.findall(r'File "([^"]+)", line \d+', tb_text)
    if not file_matches:
        return None
        
    # Check bottom-most (most specific) traceback files first
    for filepath in reversed(file_matches):
        basename = os.path.basename(filepath)
        name_no_ext = os.path.splitext(basename)[0]
        
        target_name = None
        target_type = None
        
        # 1. Match scrapers
        for s in available['scrapers']:
            if s in name_no_ext or name_no_ext in s:
                target_name = s
                target_type = "scraper"
                break
                
        # 2. Match prompts / agents
        if not target_name:
            for p in available['prompts']:
                if p in name_no_ext or name_no_ext in p:
                    target_name = p
                    target_type = "prompt"
                    break
                    
        # 3. Match optimizers
        if not target_name:
            for o in available['optimizers']:
                if o in name_no_ext or name_no_ext in o:
                    target_name = o
                    target_type = "optimizer"
                    break
                    
        if target_name and target_type:
            resolution = resolve_target(target_type, target_name)
            if resolution.get("exists"):
                return {
                    "target_type": target_type,
                    "target_name": target_name,
                    "file_path": resolution["file_path"],
                    "relative_path": resolution["relative_path"]
                }
    return None

def detect_target_from_error(error_msg: str) -> tuple[str, str] | None:
    """Fallback parser to match known error text keywords to targets."""
    available = list_available_targets()
    error_msg_lower = error_msg.lower()
    
    # 1. Match prompts/agents first
    for p in available['prompts']:
        if p in error_msg_lower:
            return "prompt", p
            
    # 2. Match scrapers
    for s in available['scrapers']:
        if s in error_msg_lower:
            return "scraper", s
            
    # 3. Match optimizers
    for o in available['optimizers']:
        if o in error_msg_lower:
            return "optimizer", o
            
    return None

def has_consecutive_failures(target_type: str, target_name: str) -> bool:
    """Check if we have failed to fix this exact target twice consecutively."""
    with get_db() as db:
        db.execute(
            """
            SELECT status FROM pending_evolution_fixes 
            WHERE target_type = %s AND target_name = %s 
            ORDER BY created_at DESC LIMIT 2
            """,
            [target_type, target_name]
        )
        rows = db.fetchall()
        # If the last two attempts both ended up rolled back or errored
        if len(rows) >= 2 and all(r[0] in ("rolled_back", "rejected") for r in rows):
            return True
    return False

# NOTE: push_git_changes() and deploy_container_nas() were removed deliberately.
#
# The watchdog used to `git add -A` (sweeping in any unrelated uncommitted work),
# commit, push, and then run `npm run deploy` to rebuild the NAS container — all
# unattended, gated only by a `py_compile` check. An LLM-authored patch could
# reach production with no test coverage and no human in the loop.
#
# Automated repair now stops at the LOG. Nothing here writes to disk at all
# since the deployer was removed 2026-07-31, so there is no patch to roll back
# and no probation to monitor. Building and shipping an image is a human
# action — do not reintroduce it here.


def write_healing_report(cycle_id: str, target_name: str, patch_id: str, success: bool, msg: str):
    """Write the details of the healing event into reports and ledgers."""
    # 1. Update cycle report
    cycle_report_path = f"reports/trading_cycle_report_{cycle_id}.md" if cycle_id else None
    if cycle_report_path and os.path.exists(cycle_report_path):
        try:
            with open(cycle_report_path, "a") as f:
                f.write(f"\n\n### 🔧 Self-Healing Action ({datetime.now(timezone.utc).isoformat()})\n")
                f.write(f"- **Target File**: `{target_name}`\n")
                f.write(f"- **Patch ID**: `{patch_id}`\n")
                f.write(f"- **Outcome**: {'✅ Success' if success else '❌ Failed'}\n")
                f.write(f"- **Notes**: {msg}\n")
            logger.info(f"Updated cycle report at {cycle_report_path}")
        except Exception as e:
            logger.error(f"Failed to append to cycle report: {e}")

    # 2. Append to verified_fixes_history.md if successful
    if success:
        history_path = "reports/verified_fixes_history.md"
        if os.path.exists(history_path):
            try:
                with open(history_path, "a") as f:
                    f.write(
                        f"| **Auto-Healed: {target_name}** | {datetime.now().strftime('%Y-%m-%d')} | **Fixed** | `{target_name}` | N/A (Auto-healed) | Deployed AI debate-approved patch. | Smoke test verification passed. |\n"
                    )
                logger.info(f"Appended success record to {history_path}")
            except Exception as e:
                logger.error(f"Failed to append to history file: {e}")

from app.services.boot_service import BootService
from app.services.startup_tasks import startup_vllm_discovery

# The last error event this watchdog acted on. get_latest_error_events always
# returns the newest error rows for the cycle, so an event that nothing
# clears (e.g. one failed analyst artifact) was re-"Detected" on every hourly
# pass — 2026-08-03 logged the same GE junior-analyst failure as a fresh
# crash five hours running. One failure, one detection.
_last_handled_event: tuple | None = None

async def heal_once():
    """One diagnosis pass. Assumes the service context is ALREADY booted.

    Split out from run_healing_cycle so the in-process scheduler can call it:
    the standalone entrypoint tears the service down in a `finally`, which would
    kill the live DB pool and scheduler if invoked from inside the running app.
    """
    # (Probation inspection removed 2026-07-31 with the evolution deployer:
    # it queried a table frozen on 07-28 and matched 0 rows every run.)
    cycle_id, status, error, phase = get_active_cycle()
    logger.info(f"Active Cycle ID: {cycle_id} | Status: {status} | Phase: {phase}")
        
    if status != "error":
        # Check if there are any worker crashes logged in the pipeline_events
        logger.info("Cycle status is not 'error'. Checking recent pipeline events for crashes...")
        error_events = get_latest_error_events(cycle_id)
        if not error_events:
            logger.info("No active pipeline event crashes found. System healthy.")
            return
        # Use the latest error event — unless it's the one we already handled.
        global _last_handled_event
        crash_event = error_events[0]
        event_key = (cycle_id, crash_event["phase"], crash_event["step"],
                     crash_event["timestamp"])
        if event_key == _last_handled_event:
            logger.info(
                "Newest error event %s/%s (%s) already handled on a previous "
                "pass — no new crashes.",
                crash_event["phase"], crash_event["step"], crash_event["timestamp"],
            )
            return
        _last_handled_event = event_key
        error_msg = crash_event["detail"]
        logger.warning(f"Detected crash event in {crash_event['phase']}/{crash_event['step']}: {error_msg}")
    else:
        error_msg = error
        logger.warning(f"Cycle in ERROR state: {error_msg}")

    if not cycle_id:
        logger.info("No active cycle found. Skipping self-healing.")
        return

    # Fetch logs from the NAS to get structured stack trace
    logs_jsonl = fetch_nas_cycle_logs(cycle_id)
        
    # ── 1. Diagnose: Find traceback in JSONL lines ──
    traceback_text = ""
    target_info = None
        
    if logs_jsonl:
        for line in reversed(logs_jsonl.splitlines()):
            if not line.strip():
                continue
            try:
                log_data = json.loads(line)
                payload = log_data.get("payload", {})
                if isinstance(payload, dict) and "stack_trace" in payload:
                    traceback_text = payload["stack_trace"]
                    logger.info("Found stack trace in cycle JSONL logs!")
                    target_info = parse_traceback_to_target(traceback_text)
                    if target_info:
                        break
            except Exception:
                pass

    # Fallback to direct text scanning if stack trace is not found in JSONL
    if not target_info:
        logger.info("Traceback mapping failed or not found. Falling back to keyword search on error message...")
        fallback = detect_target_from_error(error_msg)
        if fallback:
            target_type, target_name = fallback
            res = resolve_target(target_type, target_name)
            if res.get("exists"):
                target_info = {
                    "target_type": target_type,
                    "target_name": target_name,
                    "file_path": res["file_path"],
                    "relative_path": res["relative_path"]
                }

    # Last resort: resolve the traceback to a symbol directly.
    # Both mappers above consult target_map's hand-written dicts, so anything
    # nobody had registered simply dead-ended here — and STRATEGY_MAP is empty,
    # so no strategy failure was ever resolvable.
    if not target_info and traceback_text:
        from app.cognition.evolution.code_evidence import (
            PROJECT_ROOT,
            build_evidence_for_traceback,
        )

        evidence = build_evidence_for_traceback(traceback_text)
        if evidence:
            logger.info(
                "[SELF-HEAL] Resolved %s -> %s:%d-%d via symbol index "
                "(no target_map entry needed)",
                evidence.name, evidence.relative_path,
                evidence.lineno, evidence.end_lineno,
            )
            target_info = {
                "target_type": "symbol",
                "target_name": evidence.name,
                "file_path": str(PROJECT_ROOT / evidence.relative_path),
                "relative_path": evidence.relative_path,
                "evidence": evidence,
            }

    if not target_info:
        logger.error(f"Could not map error to any evolutionary code target. Error message: {error_msg}")
        return

    target_type = target_info["target_type"]
    target_name = target_info["target_name"]
    logger.warning(f"Target mapped successfully: {target_type}/{target_name} ({target_info['relative_path']})")

    # ── 2. Loop termination safeguard: consecutive failures check ──
    if has_consecutive_failures(target_type, target_name):
        logger.critical(
            f"⛔ HALTING SELF-HEALING: Target {target_type}/{target_name} has failed 2 consecutive fixes. Escalating to human."
        )
        write_healing_report(
            cycle_id, target_name, "N/A", False,
            "Self-healing halted due to 2 consecutive failed attempts. Intervention required."
        )
        return

    # ── 3. Hand off to the CORAL repair runner ──
    # The container does no LLM work and proposes no patch, because it cannot
    # grade one: the image ships source without .git, without the git binary,
    # and without pytest, so nothing in here can run a test or verify a diff.
    # What it did instead was run a 9-call debate whose output nobody could
    # check — measured at ~20 minutes and 47k input tokens per proposer call,
    # hourly, for proposals that were rejected 57 times out of 57.
    #
    # So it records the failure and stops. The host-side proposer that used to
    # drain this was removed 2026-07-31: it ranked patches by "the suite goes
    # green" and both of its two top-scored patches reverted a deliberate human
    # decision to satisfy a stale test. `scripts/grade_patch.py` grades a fix a
    # human wrote, keeping the worktree isolation and the fail-on-HEAD control.
    from app.cognition.evolution.coral.attempts import enqueue_job

    target_rel = target_info.get("relative_path", "")
    allowed, scope_reason = is_patchable(target_rel)
    if not allowed:
        logger.critical(
            "\u26d4 NOT QUEUED: %s is outside the self-healing scope (%s).",
            target_rel, scope_reason,
        )
        write_healing_report(
            cycle_id, target_name, "-", False,
            f"Failure in {target_rel} is outside the trading-cycle repair scope "
            f"({scope_reason}). Needs a human.",
        )
        return

    evidence = target_info.get("evidence")
    job_id = enqueue_job(
        cycle_id=cycle_id,
        error_message=error_msg or "",
        traceback_text=traceback_text or "",
        target_path=target_rel,
        target_symbol=getattr(evidence, "name", None) or target_name,
    )

    if job_id is None:
        logger.info(
            "[SELF-HEAL] %s::%s already has an open repair job \u2014 not re-queued.",
            target_rel, target_name,
        )
        return

    logger.info(
        "[SELF-HEAL] Logged failure %s for %s::%s. Nothing drains this — fix "
        "it and grade the fix with:  python scripts/grade_patch.py <branch> "
        "--repro <test>",
        job_id[:8], target_rel, target_name,
    )
    write_healing_report(
        cycle_id, target_name, job_id, True,
        f"Failure diagnosed and queued for graded repair (job {job_id[:8]}). "
        f"No patch is proposed in-container; the host runner proposes, tests, "
        f"and opens a branch only if the tests pass.",
    )


async def run_healing_cycle():
    """Standalone entrypoint: boots its own service context, then tears it down.

    Only for running this file directly. In-process callers (the scheduler) must
    use `heal_once()` — the shutdown in the `finally` below would otherwise kill
    the live DB pool and scheduler of the running service.
    """
    logger.info("Initializing BootService and vLLM discovery...")
    await BootService.startup()
    try:
        await startup_vllm_discovery()
        return await heal_once()
    finally:
        await BootService.shutdown()


if __name__ == "__main__":
    asyncio.run(run_healing_cycle())
