import os
import sys

# Must add trading-service to PYTHONPATH
sys.path.insert(0, "/home/lazycat/github/projects/sun/trading-service")

from app.db.connection import get_db

with get_db() as db:
    row = db.execute("SELECT status, cycle_id, progress, updated_at FROM pipeline_state WHERE singleton_id = 'current'").fetchone()
    print("pipeline_state:", row)
    
    cmd = db.execute("SELECT id, command_type, status FROM v3_system_commands ORDER BY created_at DESC LIMIT 5").fetchall()
    print("v3_system_commands:", cmd)
