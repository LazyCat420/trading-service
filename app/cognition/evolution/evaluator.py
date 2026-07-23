import logging
import uuid
import json
import asyncio
from typing import Any
from datetime import datetime, timezone

from app.db.connection import get_db
from app.services.prism_agent_caller import llm, Priority
from app.utils.text_utils import parse_json_response

logger = logging.getLogger(__name__)

AUDITOR_PROMPT = """You are an AI optimization expert benchmarking a multi-agent trading system.
You will be provided with a small random slice of an execution trace.

Evaluate this slice on three criteria:
1. Consistency: Did the agents follow their system prompts?
2. Focus: Did they stay on topic?
3. Directness: Did they answer the questions posed, or hallucinate?

Identify the most critical success OR failure in this slice.
Extract a single concise behavioral rule (less than 20 words).
Respond ONLY with a JSON object in this format:
{"score": 0.8, "lesson": "The rule text here"}
Where score is a float from 0.0 (total failure) to 1.0 (perfect execution).
"""

CHIEF_PROMPT = """You are the Chief Auditor of a multi-agent trading system.
Your subordinate auditors have reviewed different slices of a recent trading cycle and provided their extracted lessons and scores.

Your job is to synthesize their findings into ONE final, cohesive behavioral rule (less than 20 words) that captures the most important lesson.
Also provide a blended final score (average or weighted towards the most critical failure).
Respond ONLY with a JSON object in this format:
{"score": 0.8, "lesson": "The rule text here"}
"""

async def run_auditor(chunk: str, cycle_id: str, auditor_id: int) -> dict:
    user_prompt = f"### CYCLE TRACE SLICE\n{chunk}\n\n### EVALUATION RESULT (JSON ONLY):"
    
    response_text, _, _ = await llm.chat(
        system=AUDITOR_PROMPT,
        user=user_prompt,
        temperature=0.3,
        priority=Priority.LOW,
        agent_name=f"auditor_{auditor_id}",
        cycle_id=cycle_id,
        bot_id="system"
    )
    
    try:
        parsed = parse_json_response(response_text)
        return {
            "score": float(parsed.get("score", 0.5)),
            "lesson": parsed.get("lesson", "")
        }
    except Exception as e:
        logger.warning(f"[Auditor {auditor_id}] Failed to parse JSON: {e}")
        return {"score": 0.5, "lesson": ""}

async def run_post_cycle_evaluation(cycle_id: str):
    logger.info(f"[Evaluator] Starting random peer-to-peer audit for {cycle_id}")
    try:
        rows = None
        try:
            from app.db import mongo_store
            if mongo_store.reads_mongo("pipeline_events"):
                import random
                docs = mongo_store.find_docs(
                    "pipeline_events", {"cycle_id": cycle_id},
                    projection={"_id": 0, "phase": 1, "step": 1, "detail": 1, "status": 1},
                )
                # Same shape as the SQL: errors first, random within each group,
                # capped at 30 (3 chunks of 10).
                random.shuffle(docs)
                docs.sort(key=lambda d: 0 if (d.get("status") or "ok") != "ok" else 1)
                rows = [
                    (d.get("phase"), d.get("step"), d.get("detail"), d.get("status"))
                    for d in docs[:30]
                ]
        except Exception as me:
            logger.warning("[Evaluator] mongo events read failed, PG fallback: %s", me)
            rows = None
        if rows is None:
            with get_db() as db:
                # Fetch events. Sort by errors/failures first, then random. Limit to 30 to form 3 chunks of 10.
                rows = db.execute(
                    """
                    SELECT phase, step, detail, status
                    FROM pipeline_events
                    WHERE cycle_id = %s
                    ORDER BY
                        CASE WHEN status != 'ok' THEN 0 ELSE 1 END ASC,
                        RANDOM()
                    LIMIT 30
                    """,
                    [cycle_id]
                ).fetchall()

        if not rows:
            logger.warning(f"[Evaluator] No events found for cycle {cycle_id}. Skipping.")
            return

        trace_lines = []
        for row in rows:
            phase, step, detail, status = row
            detail = detail[:200] + "..." if len(detail) > 200 else detail
            status_flag = f" [STATUS:{status.upper()}]" if status != 'ok' else ""
            trace_lines.append(f"[{phase}][{step}]{status_flag} {detail}")
            
        # Split into up to 3 chunks for the 3 auditors
        chunk_size = max(1, len(trace_lines) // 3)
        chunks = [
            "\n".join(trace_lines[0:chunk_size]),
            "\n".join(trace_lines[chunk_size:chunk_size*2]),
            "\n".join(trace_lines[chunk_size*2:])
        ]
        
        # Filter empty chunks if we had very few rows
        chunks = [c for c in chunks if c.strip()]
        
        logger.info(f"[Evaluator] Dispatching {len(chunks)} parallel auditors...")
        
        # Run subagents in parallel
        tasks = [run_auditor(chunk, cycle_id, i+1) for i, chunk in enumerate(chunks)]
        auditor_results = await asyncio.gather(*tasks)
        
        # Filter out empty failures
        valid_results = [r for r in auditor_results if r["lesson"]]
        
        if not valid_results:
            logger.warning("[Evaluator] All auditors failed to produce a valid lesson.")
            return
            
        # Chief Auditor Synthesis
        chief_user_prompt = "### SUBORDINATE REPORTS\n"
        for i, res in enumerate(valid_results):
            chief_user_prompt += f"Auditor {i+1} (Score {res['score']}): {res['lesson']}\n"
        chief_user_prompt += "\n### CHIEF SYNTHESIS (JSON ONLY):"
        
        response_text, _, _ = await llm.chat(
            system=CHIEF_PROMPT,
            user=chief_user_prompt,
            temperature=0.2,
            priority=Priority.LOW,
            agent_name="chief_auditor",
            cycle_id=cycle_id,
            bot_id="system"
        )
        
        try:
            parsed = parse_json_response(response_text)
            final_score = float(parsed.get("score", 0.5))
            final_lesson = parsed.get("lesson", "")
            
            if final_lesson:
                with get_db() as db:
                    db.execute(
                        """
                        INSERT INTO evolution_lessons (id, session_id, round, score, status, lesson_text, timestamp)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        [
                            str(uuid.uuid4()),
                            cycle_id,
                            1,
                            final_score,
                            "audited",
                            final_lesson,
                            datetime.now(timezone.utc).isoformat()
                        ]
                    )
                logger.info(f"[Evaluator] Chief Auditor recorded lesson: {final_lesson} (Score: {final_score})")
            else:
                logger.warning(f"[Evaluator] Chief LLM returned no lesson: {response_text}")
        except json.JSONDecodeError:
            logger.error(f"[Evaluator] Failed to parse Chief LLM JSON: {response_text}")

    except Exception as e:
        logger.error(f"[Evaluator] Post-cycle evaluation failed: {e}")
