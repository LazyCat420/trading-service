import asyncio
import json
import logging
from app.db.connection import get_db
from app.autoresearch.eval_engine import process_pending_traces

logger = logging.getLogger(__name__)

async def run_autoresearch(job_id: str, payload: dict):
    logger.info("Running Autoresearch for job %s with payload %s", job_id, payload)
    
    # Phase 1: Critic (Process Evaluation) & Phase 3: Benchmark Gauntlet
    from app.autoresearch.gauntlet import run_benchmark_gauntlet
    
    gauntlet_result = await run_benchmark_gauntlet({"job_id": job_id, "payload": payload})
    
    if gauntlet_result.get("passed"):
        logger.info("Gauntlet passed! Deploying harness update.")
        from app.autoresearch.deployment import deploy_harness_update
        # Example proposed change
        deploy_harness_update(f"Job {job_id}: Ensure logic is sound and tools are used accurately.")
    else:
        logger.warning("Gauntlet failed! Initiating rollback if necessary.")
        from app.autoresearch.deployment import rollback_harness
        rollback_harness()
    
    # Process pending traces from recent cycles (part of eval_engine)
    logger.info("Processing pending traces...")
    processed_count = process_pending_traces(limit=50)
    logger.info("Processed %d traces", processed_count)
    
    with get_db() as db:
        db.execute(
            "UPDATE system_commands SET status = 'completed' WHERE id = %s",
            [job_id]
        )

async def poll_system_commands():
    logger.info("Starting autoresearch system_commands poller...")
    while True:
        try:
            with get_db() as db:
                cmd = db.execute(
                    "SELECT id, command_type, payload FROM system_commands "
                    "WHERE status = 'pending' AND command_type = 'AUTORESEARCH' "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED"
                ).fetchone()

                if cmd:
                    job_id, cmd_type, payload_str = cmd
                    logger.info("Found pending AUTORESEARCH command: %s", job_id)
                    db.execute("UPDATE system_commands SET status = 'running' WHERE id = %s", [job_id])
                    
                    payload = json.loads(payload_str) if payload_str else {}
                    
                    try:
                        await run_autoresearch(job_id, payload)
                    except Exception as e:
                        logger.error("Autoresearch failed for %s: %s", job_id, e)
                        db.execute(
                            "UPDATE system_commands SET status = 'error', payload = %s WHERE id = %s",
                            [json.dumps({"error": str(e)}), job_id]
                        )
        except Exception as e:
            logger.error("Error polling system_commands: %s", e)
        
        await asyncio.sleep(5)
