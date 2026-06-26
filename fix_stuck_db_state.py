import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db.connection import get_db

def fix_stuck_state():
    with get_db() as db:
        with db.transaction():
            print("Checking pipeline_state...")
            row = db.execute("SELECT status FROM pipeline_state WHERE singleton_id = 'current'").fetchone()
            if row and row[0] in ("starting", "running"):
                print(f"Fixing stuck pipeline_state: {row[0]} -> idle")
                db.execute("UPDATE pipeline_state SET status = 'idle' WHERE singleton_id = 'current'")
            else:
                print(f"pipeline_state is already {row[0] if row else 'empty'}")

            print("Checking v3_system_commands...")
            db.execute("UPDATE v3_system_commands SET status = 'error', error_message = 'Aborted due to stuck state recovery' WHERE status = 'running'")
            print("v3_system_commands recovered.")

            print("Checking system_commands...")
            db.execute("UPDATE system_commands SET status = 'error', error_message = 'Aborted due to stuck state recovery' WHERE status = 'running'")
            print("system_commands recovered.")

if __name__ == "__main__":
    fix_stuck_state()
