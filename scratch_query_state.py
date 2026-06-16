import sys
import os
sys.path.insert(0, os.path.abspath("."))
from app.db.connection import get_db

with get_db() as db:
    rows = db.execute("SELECT singleton_id, status, cycle_id, updated_at FROM pipeline_state LIMIT 5").fetchall()
    print("pipeline_state rows:")
    for r in rows:
        print(f" - {r}")
