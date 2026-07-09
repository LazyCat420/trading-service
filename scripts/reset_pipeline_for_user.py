import sys
from app.db.connection import get_db

with get_db() as db:
    db.execute("UPDATE pipeline_state SET status = 'idle', cycle_id = NULL, progress = '' WHERE status IN ('starting', 'running')")
    db.execute("UPDATE v3_system_commands SET status = 'error' WHERE status = 'running'")
    print("Pipeline reset and commands marked as error!")
