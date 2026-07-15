import os
import psycopg
try:
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    res = conn.execute("SELECT count(*), min(timestamp), max(timestamp) FROM pipeline_events WHERE cycle_id = 'cycle-1780618264'").fetchone()
    print("pipeline_events for cycle-1780618264:", res)
    # Also get the latest 5 events
    rows = conn.execute("SELECT timestamp, phase, step, detail, status FROM pipeline_events WHERE cycle_id = 'cycle-1780618264' ORDER BY timestamp DESC LIMIT 5").fetchall()
    print("latest events:")
    for r in rows:
        print(r)
except Exception as e:
    print("Failed to query DB:", e)
