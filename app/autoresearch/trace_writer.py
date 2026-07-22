"""Producer for agent_traces — the eval layer's input.

The historical producer (rlm_wrapper → rlm_audit) lost its only caller in the
vllm_client → lazycat-sdk migration (fa7cee3), which starved agent_traces from
2026-06-25 onward: eval_engine.process_pending_traces ran on every autoresearch
job but had nothing to grade. This module feeds the table from the live V3
tool path (base_agent's on_tool_result hook) instead of the dead V2 harness.

One row per tool call, grouped by run_id = v3:<cycle>:<ticker>:<agent>, which
mirrors the granularity the rubric was written for (per-trace scoring with
tool_result_summary / latency / stop_reason).
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Per-run loop counter so loop_step is meaningful within a run without
# needing the caller to thread state through. Keyed by run_id; pruned
# opportunistically to avoid unbounded growth across cycles.
_loop_steps: dict[str, int] = {}
_MAX_TRACKED_RUNS = 512


def write_agent_trace(
    cycle_id: str,
    ticker: str,
    agent_name: str,
    tool_name: str,
    tool_args: dict | None,
    tool_result: object,
    failed: bool,
    latency_ms: int,
) -> None:
    """Insert one agent_traces row. Never raises — telemetry must not break runs."""
    try:
        # run_id MUST be the cycle id: eval_engine.process_pending_traces zips
        # the selected t.run_id into its 'cycle_id' slot and looks up
        # decision_outcomes by it (eval_scores keys on t.id, not run_id).
        run_id = cycle_id or "nocycle"
        step_key = f"{run_id}:{ticker or '?'}:{agent_name or '?'}"

        if len(_loop_steps) > _MAX_TRACKED_RUNS:
            _loop_steps.clear()
        _loop_steps[step_key] = _loop_steps.get(step_key, 0) + 1

        try:
            args_str = json.dumps(tool_args or {}, default=str)[:2000]
        except Exception:
            args_str = str(tool_args)[:2000]
        result_summary = ("ERROR: " if failed else "") + str(tool_result)[:500]

        _rec = {
            "id": str(uuid.uuid4()), "run_id": run_id, "agent_name": agent_name,
            "task_type": "analysis", "goal": f"{ticker or '?'}: execute_task",
            "tool_name": tool_name, "tool_args": args_str, "tool_result_summary": result_summary,
            "why_tool_was_called": "agent tool call (V3 pipeline)", "tokens_before": 0, "tokens_after": 0,
            "latency_ms": int(latency_ms or 0), "loop_step": _loop_steps[step_key],
            "stop_reason": "error" if failed else "completed",
            "created_at": datetime.now(timezone.utc), "service_source": "trading-service",
        }
        with get_db() as db:
            db.execute(
                """
                INSERT INTO agent_traces (
                    id, run_id, agent_name, task_type, goal,
                    tool_name, tool_args, tool_result_summary,
                    why_tool_was_called, tokens_before, tokens_after,
                    latency_ms, loop_step, stop_reason, created_at,
                    service_source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [_rec["id"], _rec["run_id"], _rec["agent_name"], _rec["task_type"], _rec["goal"],
                 _rec["tool_name"], _rec["tool_args"], _rec["tool_result_summary"], _rec["why_tool_was_called"],
                 _rec["tokens_before"], _rec["tokens_after"], _rec["latency_ms"], _rec["loop_step"],
                 _rec["stop_reason"], _rec["created_at"], _rec["service_source"]],
            )
            db.commit()
        try:
            from app.db import mongo_store
            if mongo_store.writes_mongo("agent_traces"):
                mongo_store.insert_docs("agent_traces", [_rec])
        except Exception:
            pass
    except Exception as e:
        logger.debug("[TraceWriter] Failed to write agent trace (non-fatal): %s", e)
