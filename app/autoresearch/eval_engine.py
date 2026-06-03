import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, field_validator

from app.db.connection import get_db

logger = logging.getLogger(__name__)


class EvalStoreError(Exception):
    pass

class TraceRecord(BaseModel):
    id: str
    run_id: str
    cycle_id: Optional[str] = None
    agent_name: Optional[str] = None
    task_type: Optional[str] = None
    goal: Optional[str] = None
    planned_next_action: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Optional[str] = None
    tool_result_summary: Optional[str] = None
    why_tool_was_called: Optional[str] = None
    tokens_before: int = 0
    tokens_after: int = 0
    latency_ms: int = 0
    did_tool_change_decision: Optional[bool] = None
    loop_step: Optional[int] = None
    stop_reason: Optional[str] = None
    decision_action: Optional[str] = None
    decision_confidence: Optional[float] = 0.0
    pnl_pct: Optional[float] = 0.0

    @field_validator("decision_confidence", "pnl_pct", mode="before")
    @classmethod
    def _coerce_none_to_zero(cls, v):
        """DB columns are nullable — coerce NULL → 0.0 so downstream math works."""
        return v if v is not None else 0.0

def evaluate_trace(trace: TraceRecord) -> Dict[str, Any]:
    """Score a single trace row based on the 5-part rubric."""
    completion_score = 40.0 if trace.stop_reason == "completed" else 0.0
    
    tool_correctness = 25.0
    if trace.tool_result_summary and "error" in str(trace.tool_result_summary).lower():
        tool_correctness -= 10.0
        
    tokens_used = trace.tokens_after - trace.tokens_before
    efficiency = 20.0
    if tokens_used > 5000:
        efficiency -= 10.0
        
    recovery = 10.0
    
    stop_quality = 5.0
    if trace.stop_reason == "budget_exhausted":
        stop_quality = 0.0

    final_score = max(0.0, completion_score + tool_correctness + efficiency + recovery + stop_quality)
    
    return {
        "completion_score": completion_score,
        "tool_correctness_score": tool_correctness,
        "efficiency_score": efficiency,
        "error_recovery_score": recovery,
        "stop_quality_score": stop_quality,
        "final_score": final_score
    }

def classify_failure(trace: TraceRecord, score: Dict[str, Any]) -> str | None:
    """Classifies runs with < 70 score into failure buckets."""
    if score["final_score"] >= 70.0:
        return None
        
    if score["completion_score"] == 0 and "budget_exhausted" in (trace.stop_reason or ""):
        return "over_research"
        
    action = str(trace.decision_action or "").upper()
    
    if action == "HOLD" and trace.decision_confidence >= 60 and abs(trace.pnl_pct) > 2.0:
        return "hold_bias"
        
    tool_summary = str(trace.tool_result_summary or "").lower()
    if "error" in tool_summary or "invalid" in tool_summary:
        return "bad_arguments"
        
    if trace.tokens_after - trace.tokens_before > 8000:
        return "loop_drift"
        
    return "wrong_tool_selected"

def process_and_store_trace(trace: TraceRecord):
    """Evaluate a trace and store the score and any failure bucket."""
    score = evaluate_trace(trace)
    bucket = classify_failure(trace, score)
    
    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO eval_scores (id, run_id, completion_score, tool_correctness_score, 
                   efficiency_score, error_recovery_score, stop_quality_score, final_score)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    str(uuid.uuid4()), trace.run_id, score["completion_score"],
                    score["tool_correctness_score"], score["efficiency_score"],
                    score["error_recovery_score"], score["stop_quality_score"], score["final_score"]
                ]
            )
            
            if bucket:
                db.execute(
                    """INSERT INTO failure_buckets (id, run_id, bucket_type, description)
                       VALUES (%s, %s, %s, %s)""",
                    [str(uuid.uuid4()), trace.run_id, bucket, f"Auto-classified based on score {score['final_score']}"]
                )
    except Exception as e:
        logger.error("Failed to store eval results: %s", e)
        raise EvalStoreError(f"Failed to store eval results: {e}") from e

def evaluate_confidence_calibration(ticker: str | None = None, limit: int = 20) -> Dict[str, Any]:
    try:
        with get_db() as db:
            if ticker:
                rows = db.execute(
                    """
                    SELECT confidence, outcome, pnl_pct
                    FROM decision_outcomes
                    WHERE ticker = %s AND resolved_at IS NOT NULL
                      AND outcome IN ('WIN', 'LOSS')
                    ORDER BY resolved_at DESC LIMIT %s
                    """,
                    [ticker, limit],
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT confidence, outcome, pnl_pct
                    FROM decision_outcomes
                    WHERE resolved_at IS NOT NULL
                      AND outcome IN ('WIN', 'LOSS')
                    ORDER BY resolved_at DESC LIMIT %s
                    """,
                    [limit],
                ).fetchall()

        if len(rows) < 3:
            return {
                "calibration_score": 50.0,
                "sample_count": len(rows),
                "status": "insufficient_data",
            }

        calibration_scores = []
        win_confs = []
        loss_confs = []

        for conf, outcome, pnl_pct in rows:
            normalized_conf = (conf or 50) / 100.0
            if outcome == "WIN":
                calibration_scores.append(normalized_conf)
                win_confs.append(conf or 50)
            elif outcome == "LOSS":
                calibration_scores.append(1.0 - normalized_conf)
                loss_confs.append(conf or 50)

        if not calibration_scores:
            cal_score = 50.0
        else:
            cal_score = (sum(calibration_scores) / len(calibration_scores)) * 100

        result = {
            "calibration_score": round(cal_score, 1),
            "sample_count": len(rows),
            "status": "ok",
            "avg_confidence_on_wins": round(sum(win_confs) / len(win_confs), 1) if win_confs else None,
            "avg_confidence_on_losses": round(sum(loss_confs) / len(loss_confs), 1) if loss_confs else None,
            "win_count": len(win_confs),
            "loss_count": len(loss_confs),
        }

        logger.info(
            "Confidence calibration: %.1f%% (%d samples, %d W / %d L)",
            cal_score, len(rows), len(win_confs), len(loss_confs),
        )
        return result

    except Exception as e:
        logger.error("Confidence calibration failed: %s", e)
        return {
            "calibration_score": 50.0,
            "sample_count": 0,
            "status": f"error: {e}",
        }

def process_pending_traces(limit: int = 50) -> int:
    """Find and evaluate pending traces."""
    processed_count = 0
    with get_db() as db:
        try:
            # Join against eval_scores treating eval_scores.run_id as agent_traces.id
            rows = db.execute(
                """
                SELECT t.id, t.run_id, t.agent_name, t.task_type, t.goal, 
                       t.planned_next_action, t.tool_name, t.tool_args, 
                       t.tool_result_summary, t.why_tool_was_called, 
                       t.tokens_before, t.tokens_after, t.latency_ms, 
                       t.did_tool_change_decision, t.loop_step, t.stop_reason
                FROM agent_traces t
                LEFT JOIN eval_scores e ON t.id = e.run_id
                WHERE e.id IS NULL
                ORDER BY t.created_at ASC
                LIMIT %s
                """,
                [limit],
            ).fetchall()

            columns = [
                "id", "cycle_id", "agent_name", "task_type", "goal", 
                "planned_next_action", "tool_name", "tool_args", 
                "tool_result_summary", "why_tool_was_called", 
                "tokens_before", "tokens_after", "latency_ms", 
                "did_tool_change_decision", "loop_step", "stop_reason"
            ]

            for row in rows:
                trace = dict(zip(columns, row))
                # Map trace 'id' to 'run_id' for EvalEngine backwards compatibility
                trace["run_id"] = trace["id"]
                
                # Fetch decision info to allow hold_bias check to work
                decision = db.execute(
                    """
                    SELECT action, confidence, pnl_pct 
                    FROM decision_outcomes 
                    WHERE cycle_id = %s
                    LIMIT 1
                    """,
                    [trace.get("cycle_id")]
                ).fetchone()
                
                if decision:
                    trace["decision_action"] = decision[0] or "HOLD"
                    trace["decision_confidence"] = decision[1] or 0
                    trace["pnl_pct"] = decision[2] or 0.0
                
                try:
                    record = TraceRecord(**trace)
                    process_and_store_trace(record)
                    processed_count += 1
                except ValueError as ve:
                    logger.warning("TraceRecord validation failed for run_id %s: %s", trace.get("run_id"), ve)
                except EvalStoreError as ee:
                    logger.warning("Failed to store trace %s: %s", trace.get("run_id"), ee)

            if processed_count > 0:
                logger.info(f"[EvalWorker] Processed {processed_count} pending agent traces.")
                
        except Exception as e:
            logger.error(f"[EvalWorker] Failed to process pending traces: {e}")
            
    return processed_count

def update_tool_playbook():
    """Aggregate trace eval scores and update the tool_playbook."""
    with get_db() as db:
        try:
            # Identify successful tool sequences for playbook
            rows = db.execute(
                """
                SELECT t.agent_name, t.tool_name, COUNT(*) as uses, AVG(e.final_score) as avg_score
                FROM agent_traces t
                JOIN eval_scores e ON t.id = e.run_id
                WHERE t.tool_name IS NOT NULL
                GROUP BY t.agent_name, t.tool_name
                HAVING COUNT(*) >= 5 AND AVG(e.final_score) >= 80.0
                """
            ).fetchall()

            for agent_name, tool_name, uses, avg_score in rows:
                playbook_id = str(uuid.uuid4())
                seq = f"Primary tool: {tool_name} (avg score: {avg_score:.1f} over {uses} uses)"
                
                # Insert tool_playbook
                db.execute(
                    """
                    INSERT INTO tool_playbook (id, task_type, market_context, agent_role, recommended_tool_sequence, required_preconditions)
                    VALUES (%s, 'general', 'any', %s, %s, 'None')
                    ON CONFLICT DO NOTHING
                    """,
                    [playbook_id, agent_name, seq]
                )
                
            logger.info("[EvalWorker] Updated tool playbook based on latest eval scores.")
        except Exception as e:
            logger.error(f"[EvalWorker] Failed to update tool playbook: {e}")

async def run_eval_worker(limit: int = 50):
    """Entry point for the scheduled task."""
    logger.info("[EvalWorker] Starting evaluation sweep...")
    count = process_pending_traces(limit)
    if count > 0:
        update_tool_playbook()
    logger.info("[EvalWorker] Evaluation sweep complete.")
