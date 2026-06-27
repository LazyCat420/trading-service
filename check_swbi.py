import json
from app.db.connection import get_db

with get_db() as db:
    # 1. Let's look at the most recent gatekeeper event
    events = db.execute("""
        SELECT cycle_id, created_at, event_type, payload
        FROM pipeline_events
        WHERE event_type = 'gatekeeper_decision'
        ORDER BY created_at DESC LIMIT 1
    """).fetchall()
    
    if events:
        print("Last Gatekeeper Decision in DB:")
        print(f"Cycle: {events[0][0]} at {events[0][1]}")
        print(f"Payload: {json.dumps(events[0][3], indent=2)}")
    else:
        print("No gatekeeper_decision events found in pipeline_events.")
        
    # 2. Check the docker logs for the rationale
    import subprocess
    print("\nRecent Docker logs for Gatekeeper:")
    logs = subprocess.run(
        "docker logs trading-service | grep -i 'gatekeeper selected' | tail -n 5", 
        shell=True, capture_output=True, text=True
    )
    print(logs.stdout)
