from app.db.connection import get_db
with get_db() as db:
    db.execute("UPDATE v3_system_commands SET status = 'error' WHERE status IN ('pending', 'running')")
    db.execute("UPDATE system_commands SET status = 'error' WHERE status IN ('pending', 'running')")
    db.execute("UPDATE pipeline_state SET status = 'idle' WHERE singleton_id = 'current'")
print("Fixed DB stuck states")
