import sys
import time
import uuid
import json
sys.path.insert(0, "/home/lazycat/github/projects/sun/trading-service")
from app.db.connection import get_db

def main():
    job_id = f"job_test_{uuid.uuid4().hex[:8]}"
    payload = {
        "tickers": ["AAPL"],
        "collect": True,
        "analyze": True,
        "trade": False,
        "max_tickers": 1,
        "pipeline_version": "v3"
    }
    
    print(f"Inserting command {job_id}...")
    with get_db() as db:
        db.execute(
            "INSERT INTO v3_system_commands (id, command_type, payload, status) VALUES (%s, %s, %s, %s)",
            [job_id, "START_CYCLE", json.dumps(payload), "pending"]
        )
        
    print("Waiting 10 seconds for poller to pick it up...")
    time.sleep(10)
    
    with get_db() as db:
        row = db.execute("SELECT status, error_message, result FROM v3_system_commands WHERE id = %s", [job_id]).fetchone()
        print(f"Result for {job_id}:")
        print("Status:", row[0])
        print("Error:", row[1])
        print("Result:", row[2])

if __name__ == "__main__":
    main()
