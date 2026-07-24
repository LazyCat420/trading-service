#!/usr/bin/env python3
"""
Self-Healing Watchdog Engine
============================
Diagnoses trading-cycle failures with the Evolution Debate Council and, when
explicitly authorised, applies the resulting patch.

Two hard limits govern what this can do:

1. MODE (``SELF_HEAL_MODE``, default ``diagnose``). In ``diagnose`` the council
   runs and its proposed fix is persisted for review, but nothing is written to
   disk. ``apply`` additionally writes the patch to disk, under probation.

   Neither mode commits, pushes, or redeploys. That used to happen on every run:
   LLM-authored code could reach production behind nothing but a ``py_compile``
   check, and ``git add -A`` swept in any unrelated uncommitted work.

2. SCOPE (``repair_scope.is_patchable``). Patches may only touch trading-cycle
   source. The repair machinery, DB schema, config, deploy scripts, and tests
   are off-limits regardless of mode.

Recovery does not depend on redeploying: accepted fixes are re-applied on boot
from ``stable_harnesses``, and ``check_probation_fixes`` rolls back any that
degrade.
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
from app.cognition.evolution.debate import EvolutionDebateCouncil
from app.cognition.evolution.deployer import deploy_fix_to_disk
from app.cognition.evolution.rollback_monitor import check_probation_fixes
from app.cognition.evolution.target_map import list_available_targets, resolve_target
from app.cognition.evolution.repair_scope import is_patchable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("self_healing_watchdog")

# ── Autonomy level ────────────────────────────────────────────────────────────
# There is deliberately NO mode that redeploys the container. Patches land on
# disk and are re-applied on boot from `stable_harnesses`; probation checks roll
# them back if they degrade. Rebuilding and shipping the service stays human.
MODE_DIAGNOSE = "diagnose"   # propose only — nothing written to disk
MODE_APPLY = "apply"         # write the patch to disk, under probation + rollback
_VALID_MODES = (MODE_DIAGNOSE, MODE_APPLY)


def get_heal_mode() -> str:
    """Resolve the autonomy level. Anything unrecognised degrades to diagnose."""
    raw = (os.getenv("SELF_HEAL_MODE") or MODE_DIAGNOSE).strip().lower()
    if raw not in _VALID_MODES:
        logger.warning(
            "SELF_HEAL_MODE=%r is not one of %s — defaulting to %s.",
            raw, _VALID_MODES, MODE_DIAGNOSE,
        )
        return MODE_DIAGNOSE
    return raw

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

def get_historical_fixes_context(target_name: str) -> str:
    """Parse verified_fixes_history.md for previous attempts on target_name."""
    path = "reports/verified_fixes_history.md"
    if not os.path.exists(path):
        return ""
    
    context_lines = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # Scan table rows for target_name references
        for line in lines:
            if target_name in line and "|" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 7:
                    ref = parts[1]
                    status = parts[3]
                    failed = parts[5]
                    fixed = parts[6]
                    context_lines.append(
                        f"- Reference: {ref}\n"
                        f"  Status: {status}\n"
                        f"  Failed Attempts: {failed}\n"
                        f"  What Worked: {fixed}\n"
                    )
    except Exception as e:
        logger.warning(f"Failed to parse history ledger: {e}")
        
    if context_lines:
        return "\n── HISTORICAL REGRESSION LEDGER (Do NOT repeat failed attempts) ──\n" + "\n".join(context_lines)
    return ""

def run_syntax_check(file_path: str) -> bool:
    """Compile the Python file to ensure it has no syntax errors."""
    try:
        subprocess.run([sys.executable, "-m", "py_compile", file_path], check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Syntax compile check failed for {file_path}:\n{e.stderr.decode('utf-8')}")
        return False

# NOTE: push_git_changes() and deploy_container_nas() were removed deliberately.
#
# The watchdog used to `git add -A` (sweeping in any unrelated uncommitted work),
# commit, push, and then run `npm run deploy` to rebuild the NAS container — all
# unattended, gated only by a `py_compile` check. An LLM-authored patch could
# reach production with no test coverage and no human in the loop.
#
# Automated repair now stops at the disk write. Accepted fixes are re-applied on
# boot from `stable_harnesses` and rolled back by `check_probation_fixes` if they
# degrade, so recovery never required a redeploy in the first place. Building and
# shipping an image is a human action — do not reintroduce it here.


def run_smoke_test(ticker: str = "AAPL") -> bool:
    """Execute the cycle smoke test script to verify pipeline sanity."""
    try:
        logger.info(f"Running single-ticker smoke test for {ticker}...")
        res = subprocess.run(
            ["python", "scripts/smoke_test_cycle.py", ticker, "--timeout", "900"],
            capture_output=True, text=True
        )
        logger.info(f"Smoke test stdout:\n{res.stdout}")
        return res.returncode == 0
    except Exception as e:
        logger.error(f"Failed to run smoke test: {e}")
        return False

def trigger_cycle_resume():
    """Send system command to database to resume the active cycle."""
    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO system_commands (id, command_type, payload, status, created_at)
                VALUES (%s, 'RESUME_INTERRUPTED', '{}', 'pending', CURRENT_TIMESTAMP)
                """,
                [f"cmd-resume-{int(datetime.now().timestamp())}"]
            )
        logger.info("Successfully queued RESUME_INTERRUPTED system command to DB.")
    except Exception as e:
        logger.error(f"Failed to queue resume command: {e}")

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

async def heal_once():
    """One diagnosis pass. Assumes the service context is ALREADY booted.

    Split out from run_healing_cycle so the in-process scheduler can call it:
    the standalone entrypoint tears the service down in a `finally`, which would
    kill the live DB pool and scheduler if invoked from inside the running app.
    """
    mode = get_heal_mode()
    logger.info("Self-healing mode: %s", mode)
    logger.info("=" * 60)
    logger.info("INSPECTING PROBATIONARY FIXES")
    logger.info("=" * 60)
    probation_summary = check_probation_fixes(current_cycle_id="current")
    logger.info(f"Probation summary: {probation_summary}")

    cycle_id, status, error, phase = get_active_cycle()
    logger.info(f"Active Cycle ID: {cycle_id} | Status: {status} | Phase: {phase}")
        
    if status != "error":
        # Check if there are any worker crashes logged in the pipeline_events
        logger.info("Cycle status is not 'error'. Checking recent pipeline events for crashes...")
        error_events = get_latest_error_events(cycle_id)
        if not error_events:
            logger.info("No active pipeline event crashes found. System healthy.")
            return
        # Use the latest error event
        crash_event = error_events[0]
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

    # ── 3. Generate Patch via Debate Council ──
    logger.info("Triggering Evolution Debate Council...")
        
    # Pull historical fixes context for this target name
    history_context = get_historical_fixes_context(target_name)
    issue_desc = f"EXCEPTION / ERROR DETAIL:\n{error_msg}\n\nTRACEBACK:\n{traceback_text or 'Not available'}"
    if history_context:
        issue_desc += f"\n\n{history_context}"
        logger.info("Injected historical regression memory context into the debate coordinator prompt.")

    council = EvolutionDebateCouncil()
    debate_res = await council.run_debate(
        cycle_id=cycle_id,
        target_type=target_type,
        target_name=target_name,
        issue_description=issue_desc
    )

    if not debate_res or debate_res.get("status") != "pending":
        logger.error(f"Debate Council rejected proposed patches or failed. Status: {debate_res.get('status') if debate_res else 'None'}")
        return

    fix_id = debate_res["fix_id"]

    # ── 3b. Scope gate: trading-cycle source only ──
    # Checked after the council proposes and before anything touches disk,
    # so an out-of-scope proposal is recorded and refused rather than applied.
    target_rel = target_info.get("relative_path", "")
    allowed, scope_reason = is_patchable(target_rel)
    if not allowed:
        logger.critical(
            "⛔ REFUSING PATCH: %s is outside the self-healing scope (%s). "
            "Fix %s left pending for human review.",
            target_rel, scope_reason, fix_id,
        )
        with get_db() as db:
            db.execute(
                "UPDATE pending_evolution_fixes SET status = 'rejected', "
                "failure_reason = %s WHERE id = %s",
                [f"Out of self-healing scope: {scope_reason}", fix_id],
            )
        write_healing_report(
            cycle_id, target_name, fix_id, False,
            f"Patch refused — {target_rel} is outside the trading-cycle "
            f"repair scope ({scope_reason}). Needs a human.",
        )
        return

    # ── 4. Apply Patch to Disk (mode-gated) ──
    if mode == MODE_DIAGNOSE:
        logger.info(
            "🔍 SELF_HEAL_MODE=diagnose — fix %s proposed for %s and left "
            "pending. Nothing written to disk. Set SELF_HEAL_MODE=apply to "
            "let the watchdog apply it.",
            fix_id, target_rel,
        )
        write_healing_report(
            cycle_id, target_name, fix_id, True,
            "Diagnosed and proposed a patch (diagnose mode — not applied).",
        )
        return

    logger.info(f"Approved fix ID: {fix_id}. Applying patch locally...")
    deploy_res = deploy_fix_to_disk(fix_id)
    if "error" in deploy_res:
        logger.error(f"Deployment to local disk failed: {deploy_res['error']}")
        return
    logger.info(f"Patch deployed locally. Backup saved at {deploy_res.get('backup_path')}")

    # ── 4b. Syntax Compile Check ──
    file_path = deploy_res.get("file_path")
    if file_path and file_path.endswith(".py"):
        if not run_syntax_check(file_path):
            logger.error("🔴 Syntax compile check FAILED for the proposed patch. Rolling back to backup immediately.")
            from app.cognition.evolution.deployer import rollback_fix
            rollback_fix(fix_id)
            with get_db() as db:
                db.execute(
                    "UPDATE pending_evolution_fixes SET status = 'rejected', failure_reason = %s WHERE id = %s",
                    ["SyntaxError: Proposed patch failed syntax compile check", fix_id]
                )
            return
        logger.info("🟢 Syntax compile check passed for the proposed patch.")

    # ── 5. Verification & Resume ──
    # No git push and no container rebuild: the patch is live on disk for
    # this process, will be re-applied on boot from `stable_harnesses` if it
    # proves out, and `check_probation_fixes` rolls it back if it degrades.
    # Shipping a new container image remains a human decision.
    smoke_pass = run_smoke_test(ticker="AAPL")
    if smoke_pass:
        logger.info("🟢 Smoke test passed! Resuming cycle...")
        trigger_cycle_resume()
        write_healing_report(
            cycle_id, target_name, fix_id, True,
            "Debate-approved patch applied to trading-cycle source and verified "
            "via smoke test. On probation; not committed or deployed."
        )
    else:
        logger.error("🔴 Smoke test FAILED after applying the fix. The fix will remain in probation or rollback on next check.")
        write_healing_report(
            cycle_id, target_name, fix_id, False,
            "Patch applied but post-deploy smoke test failed. Fix remains in probation."
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
