import logging
import uuid
import json
from typing import Any
from datetime import datetime, timezone

from app.db.connection import get_db
from app.services.prism_agent_caller import llm, Priority

logger = logging.getLogger(__name__)

EVALUATOR_PROMPT = """You are an AI optimization expert benchmarking multi-agent trading systems.
You will be provided with an execution trace (log of events) from a recently completed trading cycle.

Your task is to review the events and benchmark the agents' performance on three criteria:
1. Consistency: Did the agents follow their system prompts?
2. Focus: Did they stay on topic?
3. Directness: Did they actually answer the questions posed, or did they hallucinate?

Identify the most critical success OR the most critical failure in this cycle.
Then, extract a single, concise behavioral rule (less than 20 words) that the system must follow in the future to replicate the success or avoid the failure.
Respond ONLY with a JSON object in this format:
{"score": 0.8, "lesson": "The rule text here"}
Where score is a float from 0.0 (total failure) to 1.0 (perfect execution).
"""

async def run_post_cycle_evaluation(cycle_id: str):
    """
    Runs automatically after a trading cycle.
    Fetches the cycle's execution trace and uses an LLM to evaluate it,
    storing the resulting lesson in the evolution_lessons table.
    """
    logger.info(f"[Evaluator] Starting post-cycle evaluation for {cycle_id}")
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT phase, step, detail FROM pipeline_events WHERE cycle_id = %s ORDER BY timestamp ASC",
                [cycle_id]
            ).fetchall()
            
        if not rows:
            logger.warning(f"[Evaluator] No events found for cycle {cycle_id}. Skipping.")
            return

        # Prepare trace
        trace_lines = []
        for row in rows:
            phase, step, detail = row
            # Truncate details that are too long
            detail = detail[:300] + "..." if len(detail) > 300 else detail
            trace_lines.append(f"[{phase}][{step}] {detail}")
            
        # If trace is massive, keep only first and last parts to save tokens
        if len(trace_lines) > 500:
            trace_lines = trace_lines[:250] + ["... [TRUNCATED] ..."] + trace_lines[-250:]
            
        trace_text = "\n".join(trace_lines)
        user_prompt = f"### CYCLE TRACE ({cycle_id})\n{trace_text}\n\n### EVALUATION RESULT (JSON ONLY):"

        response_text, _, _ = await llm.chat(
            system=EVALUATOR_PROMPT,
            user=user_prompt,
            temperature=0.3,
            priority=Priority.LOW,
            agent_name="post_cycle_evaluator",
            cycle_id=cycle_id,
            bot_id="system"
        )
        
        # Parse JSON manually to avoid dependencies
        try:
            # simple cleanup
            clean_text = response_text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean_text)
            score = float(parsed.get("score", 0.5))
            lesson = parsed.get("lesson", "")
            
            if lesson:
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
                            score,
                            "evaluated",
                            lesson,
                            datetime.now(timezone.utc).isoformat()
                        ]
                    )
                logger.info(f"[Evaluator] Recorded lesson for {cycle_id}: {lesson} (Score: {score})")
            else:
                logger.warning(f"[Evaluator] LLM returned no lesson: {response_text}")
        except json.JSONDecodeError:
            logger.error(f"[Evaluator] Failed to parse LLM evaluation JSON: {response_text}")

    except Exception as e:
        logger.error(f"[Evaluator] Post-cycle evaluation failed: {e}")
