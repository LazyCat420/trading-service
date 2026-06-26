from app.db.connection import get_db
import json
with get_db() as db:
    rows = db.execute("SELECT command_type, status, error_message, created_at, completed_at FROM v3_system_commands ORDER BY created_at DESC LIMIT 5").fetchall()
    for r in rows:
        print(r)
