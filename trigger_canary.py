import psycopg
import json
import time
import uuid
import os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot")

def trigger_canary():
    cycle_id = f"canary_v3_{uuid.uuid4().hex[:8]}"
    print(f"Triggering manual canary cycle: {cycle_id}")
    
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
        "cycle_id": cycle_id
    }
    
    cur.execute(
        "INSERT INTO v3_system_commands (id, command_type, payload) VALUES (%s, %s, %s);",
        (cycle_id, "START_V3_CYCLE", json.dumps(payload))
    )
    
    print("Canary cycle triggered. Waiting for SharedDesk phase updates...")
    for _ in range(120): # 10 minutes max
        time.sleep(5)
        cur.execute("SELECT phase, desk_data FROM shared_desk WHERE cycle_id = %s;", (cycle_id,))
        row = cur.fetchone()
        if not row:
            continue
            
        phase, desk_data_str = row
        desk_data = desk_data_str if isinstance(desk_data_str, dict) else json.loads(desk_data_str) if desk_data_str else {}
        
        bull = desk_data.get("bull_argument")
        bear = desk_data.get("bear_rebuttal")
        regime = desk_data.get("regime_classification")
        decision = desk_data.get("final_decision")
        outcomes = desk_data.get("phase_outcomes", {})
        
        print(f"Phase: {phase}")
        if phase in ("PM_DONE", "ABORTED"):
            print("Cycle Finished.")
            print(f"Bull: {bull is not None}, Bear: {bear is not None}, Regime: {regime is not None}")
            print(f"Decision: {decision}")
            print(f"Outcomes: {outcomes}")
            
            # Wait for pipeline_state to be 'done' to prevent race conditions with the next cycle
            print("Waiting for PipelineService to finalize 'done' state...")
            for _ in range(30):
                time.sleep(2)
                cur.execute("SELECT status FROM pipeline_state WHERE singleton_id = 'current';")
                state_row = cur.fetchone()
                if state_row and state_row[0] == 'done':
                    print("Pipeline finalized.")
                    break
            break
            
    conn.close()

if __name__ == "__main__":
    trigger_canary()
