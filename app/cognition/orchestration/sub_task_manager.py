import logging
import json
import asyncio
from typing import Any
from app.db.connection import get_db
from app.services.prism_agent_caller import call_prism_agent
from app.services.vllm_client import Priority

logger = logging.getLogger(__name__)

def enqueue_sub_task(parent_agent: str, sub_agent: str, ticker: str, payload: dict) -> int:
    """Enqueue a sub-task into the database."""
    try:
        with get_db() as db:
            result = db.execute(
                """
                INSERT INTO sub_task_queue (parent_agent, sub_agent, ticker, task_payload)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (parent_agent, sub_agent, ticker, json.dumps(payload))
            ).fetchone()
            if result:
                logger.info(f"[SUB-TASK] Enqueued task {result[0]} for {sub_agent} on {ticker}")
                return result[0]
    except Exception as e:
        logger.error(f"[SUB-TASK] Failed to enqueue sub-task: {e}")
    return -1

async def process_sub_task(task_id: int, sub_agent: str, ticker: str, payload: dict):
    """Process a single sub-task using Prism."""
    logger.info(f"[SUB-TASK] Processing task {task_id} for {sub_agent} on {ticker}")
    try:
        user_message = payload.get("message", "Please analyze the provided data.")
        
        response, _, _ = await call_prism_agent(
            agent_id=sub_agent.upper(),
            user_message=user_message,
            fallback_system_prompt="You are a delegated sub-agent.",
            fallback_agent_name=sub_agent.lower(),
            priority=Priority.NORMAL,
            ticker=ticker,
            actor_label=f"subtask_{task_id}"
        )
        
        with get_db() as db:
            db.execute(
                "UPDATE sub_task_queue SET status = 'completed', result = %s, updated_at = NOW() WHERE id = %s",
                (json.dumps({"response": response}), task_id)
            )
            
            # If this was a critic, save the feedback directly to the audit table
            if sub_agent.upper() == "CRITIC_AGENT":
                try:
                    import re
                    # Extract JSON from response
                    json_match = re.search(r'\{.*\}', response, re.DOTALL)
                    if json_match:
                        parsed = json.loads(json_match.group(0))
                        db.execute(
                            """
                            INSERT INTO critic_feedback (ticker, target_agent, score, hallucinations, missing_risks)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                ticker,
                                payload.get("target_agent", "UNKNOWN"),
                                parsed.get("score"),
                                json.dumps(parsed.get("hallucinations", [])),
                                json.dumps(parsed.get("missing_risks", []))
                            )
                        )
                except Exception as e:
                    logger.error(f"[SUB-TASK] Failed to parse critic feedback: {e}")
                    
        logger.info(f"[SUB-TASK] Completed task {task_id}")
    except Exception as e:
        logger.error(f"[SUB-TASK] Error processing task {task_id}: {e}")
        with get_db() as db:
            db.execute(
                "UPDATE sub_task_queue SET status = 'failed', result = %s, updated_at = NOW() WHERE id = %s",
                (json.dumps({"error": str(e)}), task_id)
            )

async def poll_sub_tasks():
    """Background loop to poll and process sub-tasks."""
    logger.info("[SUB-TASK] Started background sub-task polling loop.")
    while True:
        try:
            tasks_to_process = []
            with get_db() as db:
                # Claim up to 5 pending tasks
                db.execute("BEGIN")
                rows = db.execute(
                    """
                    SELECT id, sub_agent, ticker, task_payload 
                    FROM sub_task_queue 
                    WHERE status = 'pending' 
                    FOR UPDATE SKIP LOCKED 
                    LIMIT 5
                    """
                ).fetchall()
                
                for r in rows:
                    db.execute("UPDATE sub_task_queue SET status = 'processing', updated_at = NOW() WHERE id = %s", (r[0],))
                    tasks_to_process.append(r)
                db.execute("COMMIT")
            
            if tasks_to_process:
                # Process concurrently
                coroutines = [
                    process_sub_task(r[0], r[1], r[2], r[3] if isinstance(r[3], dict) else json.loads(r[3]))
                    for r in tasks_to_process
                ]
                await asyncio.gather(*coroutines)
            else:
                await asyncio.sleep(5)
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[SUB-TASK] Error in poll loop: {e}")
            await asyncio.sleep(5)
