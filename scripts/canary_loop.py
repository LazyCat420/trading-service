import psycopg
import json
import time
import uuid
import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot"

def trigger_canary():
    job_id = f"canary_{uuid.uuid4().hex[:8]}"
    print(f"Triggering canary cycle under job ID: {job_id}")
    
    conn = psycopg.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    
    payload = {
        "trade": False,
        "analyze": True,
        "collect": False,
        "tickers": ["AAPL"],
        "max_tickers": 1,
        "start_fresh": True,
        "pipeline_version": "v3"
    }
    
    cur.execute(
        "INSERT INTO v3_system_commands (id, command_type, payload) VALUES (%s, %s, %s);",
        (job_id, "START_V3_CYCLE", json.dumps(payload))
    )
    
    print("Command inserted. Waiting for cycle_id...")
    
    max_wait = 120
    cycle_id = None
    for i in range(max_wait):
        time.sleep(10)
        cur.execute("SELECT status, result, error_message FROM v3_system_commands WHERE id = %s;", (job_id,))
        row = cur.fetchone()
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
                cur.execute("SELECT cycle_id FROM pipeline_state WHERE singleton_id = 'current' AND status IN ('running', 'blocked');")
                running_row = cur.fetchone()
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
        cur.execute("SELECT phase, desk_data FROM shared_desk WHERE cycle_id = %s AND ticker = 'AAPL';", (cycle_id,))
        row = cur.fetchone()
        if not row:
            continue
        phase, data_val = row
        desk_data = json.loads(data_val) if isinstance(data_val, str) else data_val
        desk_phase = desk_data.get("phase", phase)
        print(f"Current phase: {desk_phase}")
        if desk_phase in ("PM_DONE", "ABORTED", "FAILED"):
            break
            
    if not desk_data:
        print("FAILED: No SharedDesk appears in postgres within 5 minutes")
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
