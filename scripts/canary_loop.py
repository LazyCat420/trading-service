"""Canary: trigger one cycle, watch it, and grade the desk it produces.

Converted off Postgres 2026-08-30. The INSERT below used to land on the frozen
archive, i.e. on a queue `cycle_main.poll_system_commands` does not drain, so
the canary could never have fired — it would have timed out waiting for a
cycle_id that nothing was ever going to write. Enqueue now goes through
`app.services.cycle_queue`, the one writer.
"""
import json
import time

from app.db import mongo_query
from app.services.cycle_queue import enqueue_start_cycle


def trigger_canary():
    payload = {
        "trade": False,
        "analyze": True,
        "collect": False,
        "tickers": ["AAPL"],
        "max_tickers": 1,
        "start_fresh": True,
        "pipeline_version": "v3"
    }
    job_id = enqueue_start_cycle(payload, prefix="canary")
    print(f"Triggering canary cycle under job ID: {job_id}")

    print("Command queued. Waiting for cycle_id...")
    
    max_wait = 120
    cycle_id = None
    for i in range(max_wait):
        time.sleep(10)
        row = mongo_query.find_row(
            "v3_system_commands", {"id": job_id},
            ["status", "result", "error_message"],
        )
        if not row:
            continue
        status, result_val, err_msg = row
        if status in ("completed", "error"):
            if status == "error":
                print(f"FAILED: Failed to trigger: {err_msg}")
                return
            result = json.loads(result_val) if isinstance(result_val, str) else result_val
            if result and result.get("status") == "deduplicated":
                print("Cycle deduplicated. Fetching currently running cycle...")
                running_row = mongo_query.find_row(
                    "pipeline_state",
                    {"singleton_id": "current", "status": {"$in": ["running", "blocked"]}},
                    ["cycle_id"],
                )
                if running_row and running_row[0]:
                    cycle_id = running_row[0]
                    print(f"Monitoring existing cycle: {cycle_id}")
                    break
                else:
                    print("FAILED: Deduplicated but no running cycle found")
                    return
            cycle_id = result.get("cycle_id")
            print(f"Trigger succeeded. Cycle ID: {cycle_id}")
            break
            
    if not cycle_id:
        print("FAILED: Timeout waiting for command trigger")
        return

    print(f"Waiting for SharedDesk document for {cycle_id}...")
    start_time = time.time()
    desk_data = None
    while time.time() - start_time < 3600: # 60 mins
        time.sleep(5)
        row = mongo_query.find_row(
            "shared_desk", {"cycle_id": cycle_id, "ticker": "AAPL"},
            ["phase", "desk_data"],
        )
        if not row:
            continue
        phase, data_val = row
        desk_data = json.loads(data_val) if isinstance(data_val, str) else data_val
        desk_phase = desk_data.get("phase", phase)
        print(f"Current phase: {desk_phase}")
        if desk_phase in ("PM_DONE", "ABORTED", "FAILED"):
            break
            
    if not desk_data:
        print("FAILED: no SharedDesk document appeared in Mongo within the wait window")
        return
        
    desk_phase = desk_data.get("phase")
    final_decision = desk_data.get("final_decision", {}) or {}
    action = final_decision.get("action")
    confidence = final_decision.get("confidence", 0)
    reasoning = final_decision.get("reasoning")
    bull_argument = desk_data.get("bull_argument")
    bear_rebuttal = desk_data.get("bear_rebuttal")
    regime_classification = desk_data.get("regime_classification")
    phase_outcomes = desk_data.get("phase_outcomes", {})
    
    has_timeout_or_error = any(outcome in ("TIMED_OUT", "AGENT_ERROR") for outcome in phase_outcomes.values())
    has_critical_timeout = any(phase_outcomes.get(p) == "TIMED_OUT" for p in ["bull_argument", "bear_rebuttal", "board_of_directors"])
    
    status = "SUCCESS"
    if desk_phase == "ABORTED" or has_critical_timeout:
        status = "FAILED"
    elif desk_phase == "PM_DONE" and confidence == 0:
        status = "DEGRADED"
    elif not (bull_argument and bear_rebuttal and regime_classification):
        status = "DEGRADED"
    elif has_timeout_or_error:
        status = "DEGRADED"
        
    if status == "SUCCESS":
        if not (desk_phase == "PM_DONE" and action and reasoning and confidence > 0):
            status = "FAILED"
            
    # HEALTH REPORT
    report = {
        "status": status,
        "phases_completed": list(phase_outcomes.keys()),
        "final_action": action,
        "confidence": confidence,
        "regime": regime_classification.get("regime") if regime_classification else None,
        "total_time_s": int(time.time() - start_time)
    }
    print("\n--- HEALTH REPORT ---")
    print(json.dumps(report, indent=2))
    print(f"\nFINAL_STATUS: {status}")

if __name__ == "__main__":
    trigger_canary()
