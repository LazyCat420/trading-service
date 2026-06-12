from app.db.connection import get_db
with get_db() as db:
    res = db.execute("SELECT timestamp, step, detail FROM pipeline_events WHERE phase='trading' ORDER BY timestamp DESC LIMIT 20").fetchall()
    for r in res:
        print(r)
