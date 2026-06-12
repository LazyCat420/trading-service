from app.db.connection import get_db
with get_db() as db:
    print(db.execute("SELECT status, primary_failure_reason FROM pipeline_state WHERE singleton_id='current'").fetchone())
