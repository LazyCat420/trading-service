import psycopg
import json
import time
import uuid

def run_test():
    job_id = f"test_audit_{uuid.uuid4().hex[:8]}"
    print(f"Triggering manual cycle under job ID: {job_id}")
    
    import os
    from app.config import settings
    
    # Extract connection args from URL or fallback to settings if possible.
    # We will just use the same psycopg connect pattern but with the DB URL
    conn = psycopg.connect(settings.DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    
    # Payload for only running analysis phase on PLTR
    payload = {
        "trade": False,
        "analyze": True,
        "collect": False,
        "tickers": ["PLTR"],
        "max_tickers": 1,
        "start_fresh": True
    }
    
    cur.execute(
        "INSERT INTO v3_system_commands (id, command_type, payload) VALUES (%s, %s, %s);",
        (job_id, "START_CYCLE", json.dumps(payload))
    )
    conn.commit()
    print("Command inserted. Waiting for processing...")
    
    cycle_id = None
    # Poll v3_system_commands for completion of the trigger itself
    for _ in range(30):
        time.sleep(1)
        cur.execute("SELECT status, result, error_message FROM v3_system_commands WHERE id = %s;", (job_id,))
        row = cur.fetchone()
        if not row:
            continue
        status, result_val, err_msg = row
        print(f"Trigger Status: {status}")
        if status in ("completed", "error"):
            if status == "error":
                print(f"Failed to trigger: {err_msg}")
                conn.close()
                return
            result = json.loads(result_val) if isinstance(result_val, str) else result_val
            cycle_id = result.get("cycle_id")
            print(f"Trigger succeeded. Cycle ID: {cycle_id}")
            break
    
    if not cycle_id:
        print("Timeout waiting for command trigger")
        conn.close()
        return

    # Now poll pipeline_state table for the status of the cycle itself
    print(f"Polling pipeline state for {cycle_id}...")
    for _ in range(60):
        time.sleep(5)
        cur.execute("SELECT status, phase, progress, error FROM pipeline_state WHERE cycle_id = %s;", (cycle_id,))
        row = cur.fetchone()
        if not row:
            print(f"No pipeline state found for {cycle_id} yet...")
            continue
        status, phase, progress, err = row
        print(f"Cycle: {cycle_id} | Status: {status} | Phase: {phase} | Progress: {progress}% | Error: {err}")
        if status in ("done", "error", "failed", "cancelled"):
            break
            
    # Check error logs
    print("\n--- Checking Cycle Error Logs ---")
    cur.execute(
        "SELECT phase, ticker, error_type, error_message, stack_trace FROM execution_errors WHERE cycle_id = %s;",
        (cycle_id,)
    )
    errors = cur.fetchall()
    if not errors:
        print("No errors logged for this cycle.")
    for err_row in errors:
        print(f"Phase: {err_row[0]} | Ticker: {err_row[1]} | Error Type: {err_row[2]}")
        print(f"Message: {err_row[3]}")
        if err_row[4]:
            print(f"Stack trace: {err_row[4][:300]}...")
        print("-" * 50)
        
    # Check analysis results
    print("\n--- Checking Analysis Results for PLTR ---")
    cur.execute(
        """
        SELECT ticker, agent_name, confidence, triage_tier, thesis_verdict, 
               thesis_confidence, thesis_summary, thesis_unchanged, created_at 
        FROM analysis_results 
        WHERE cycle_id = %s;
        """,
        (cycle_id,)
    )
    results = cur.fetchall()
    if not results:
        print("No analysis results found in database for this cycle.")
    for res in results:
        print(f"Ticker: {res[0]} | Agent: {res[1]} | Confidence: {res[2]} | Tier: {res[3]}")
        print(f"Thesis Verdict: {res[4]} | Thesis Confidence: {res[5]}")
        print(f"Thesis Summary: {res[6]}")
        print(f"Thesis Unchanged: {res[7]} | Created At: {res[8]}")
        print("-" * 50)
        
    conn.close()

if __name__ == "__main__":
    run_test()
