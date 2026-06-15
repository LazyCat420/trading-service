#!/usr/bin/env python3
"""
Self-Healing Watchdog Engine
============================
Integrates the Evolution Debate Council and container deployments
into the trading cycle schedule loop to autonomously heal pipeline errors.
"""

import sys
import os
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
            events.append({
                "phase": row[0],
                "step": row[1],
                "detail": row[2],
                "timestamp": row[3]
            })
    return events

def fetch_nas_cycle_logs(cycle_id: str) -> str:
    """Fetch the JSONL log file for the cycle directly from the NAS."""
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

def push_git_changes() -> bool:
    """Push code changes to GitHub."""
    try:
        logger.info("Committing and pushing self-healing changes to GitHub...")
        subprocess.run(["git", "add", "-A"], check=True)
        # Check if there are changes staged
        status_res = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if status_res.returncode == 0:
            logger.info("No changes to commit/push.")
            return True
            
        subprocess.run(["git", "commit", "-m", "chore: auto-applied self-healing code patch"], check=True)
        # Dynamically detect active branch
        branch_res = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, check=True)
        active_branch = branch_res.stdout.strip() or "master"
        logger.info(f"Pushing to origin {active_branch}...")
        subprocess.run(["git", "push", "origin", active_branch], check=True)
        return True
    except Exception as e:
        logger.error(f"Git push failed: {e}")
        return False

def deploy_container_nas() -> bool:
    """Rebuild and redeploy the NAS container."""
    try:
        logger.info("Redeploying NAS container using npm run deploy...")
        res = subprocess.run(["npm", "run", "deploy"], capture_output=True, text=True)
        if res.returncode == 0:
            logger.info("NAS container successfully redeployed.")
            return True
        else:
            logger.error(f"Container deployment failed:\n{res.stderr or res.stdout}")
            return False
    except Exception as e:
        logger.error(f"Failed to execute NAS container deploy: {e}")
        return False

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

async def run_healing_cycle():
    logger.info("Initializing BootService and vLLM discovery...")
    await BootService.startup()
    try:
        await startup_vllm_discovery()

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
        logger.info(f"Approved fix ID: {fix_id}. Applying patch locally...")

        # ── 4. Apply Patch to Disk ──
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

        # ── 5. Git Push and NAS Re-deploy ──
        if not push_git_changes():
            logger.error("Git push of evolutionary fix failed!")
            return

        if not deploy_container_nas():
            logger.error("NAS container redeployment failed!")
            return

        # ── 6. Verification & Resume ──
        smoke_pass = run_smoke_test(ticker="AAPL")
        if smoke_pass:
            logger.info("🟢 Smoke test passed post-deploy! Resuming cycle...")
            trigger_cycle_resume()
            write_healing_report(
                cycle_id, target_name, fix_id, True,
                "Debate approved patch successfully deployed, git pushed, container rebuilt on NAS, and verified via smoke test."
            )
        else:
            logger.error("🔴 Smoke test FAILED after applying the fix. The fix will remain in probation or rollback on next check.")
            write_healing_report(
                cycle_id, target_name, fix_id, False,
                "Debate approved patch was deployed, but post-deploy smoke test failed. Fix remains in probation."
            )
    finally:
        await BootService.shutdown()

if __name__ == "__main__":
    asyncio.run(run_healing_cycle())
