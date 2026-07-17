import asyncio
import json
import logging
from app.db.connection import get_db
from app.autoresearch.eval_engine import process_pending_traces

logger = logging.getLogger(__name__)

async def run_autoresearch(job_id: str, payload: dict):
    logger.info("Running Autoresearch for job %s with payload %s", job_id, payload)
    
    # ── Run Core Autoresearch Audit & Reports ──
    cycle_id = payload.get("cycle_id")
    cycle_summary = payload.get("cycle_summary")
    if cycle_id and cycle_summary:
        from app.autoresearch.core import run_autoresearch as run_autoresearch_core
        try:
            logger.info("Running full Autoresearch audit report for cycle %s", cycle_id)
            await run_autoresearch_core(cycle_id, cycle_summary)
        except Exception as e:
            logger.error("Failed running core run_autoresearch: %s", e)
            raise Exception(f"Core run_autoresearch failed: {e}")
    
    # Process pending traces from recent cycles (part of eval_engine)
    logger.info("Processing pending traces...")
    processed_count = process_pending_traces(limit=50)
    logger.info("Processed %d traces", processed_count)

    # Aggregate trace scores into the tool playbook. This was defined but
    # never scheduled (its only caller, run_eval_worker, had no scheduler),
    # so eval_scores was write-only and tool_playbook stayed empty forever.
    try:
        from app.autoresearch.eval_engine import update_tool_playbook
        update_tool_playbook()
    except Exception as pb_err:
        logger.warning("update_tool_playbook failed (non-fatal): %s", pb_err)
    
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed' WHERE id = %s",
            [job_id]
        )

async def run_deploy_fix(job_id: str, payload: dict):
    fix_id = payload.get("fix_id")
    if not fix_id:
        raise ValueError("Missing fix_id in payload")
    
    from app.cognition.evolution.deployer import deploy_fix_to_disk
    logger.info("Deploying fix %s on local disk...", fix_id)
    res = deploy_fix_to_disk(fix_id)
    if "error" in res:
        raise Exception(res["error"])
        
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed', payload = %s WHERE id = %s",
            [json.dumps(res), job_id]
        )

async def run_rollback_fix(job_id: str, payload: dict):
    fix_id = payload.get("fix_id")
    if not fix_id:
        raise ValueError("Missing fix_id in payload")
    
    from app.cognition.evolution.deployer import rollback_fix
    logger.info("Rolling back fix %s...", fix_id)
    res = rollback_fix(fix_id)
    if "error" in res:
        raise Exception(res["error"])
        
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed', payload = %s WHERE id = %s",
            [json.dumps(res), job_id]
        )

async def poll_system_commands():
    logger.info("Starting autoresearch system_commands poller...")
    while True:
        try:
            with get_db() as db:
                cmd = db.execute(
                    "SELECT id, command_type, payload FROM system_commands "
                    "WHERE status = 'pending' AND command_type IN ('AUTORESEARCH', 'DEPLOY_FIX', 'ROLLBACK_FIX') "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED"
                ).fetchone()

                if cmd:
                    job_id, cmd_type, payload_str = cmd
                    logger.info("Found pending %s command: %s", cmd_type, job_id)
                    db.execute("UPDATE system_commands SET status = 'running' WHERE id = %s", [job_id])
                    
                    payload = json.loads(payload_str) if payload_str else {}
                    
                    try:
                        if cmd_type == "AUTORESEARCH":
                            await run_autoresearch(job_id, payload)
                        elif cmd_type == "DEPLOY_FIX":
                            await run_deploy_fix(job_id, payload)
                        elif cmd_type == "ROLLBACK_FIX":
                            await run_rollback_fix(job_id, payload)
                    except Exception as e:
                        logger.error("%s failed for %s: %s", cmd_type, job_id, e)
                        db.execute(
                            "UPDATE system_commands SET status = 'error', payload = %s WHERE id = %s",
                            [json.dumps({"error": str(e)}), job_id]
                        )
        except Exception as e:
            logger.error("Error polling system_commands: %s", e)
        
        await asyncio.sleep(5)

