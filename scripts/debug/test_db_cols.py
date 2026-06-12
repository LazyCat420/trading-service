def check():
    from app.db.connection import get_db
    with get_db() as db:
        res = db.execute("SELECT column_name FROM information_schema.columns WHERE table_name='pipeline_state'").fetchall()
        print([r[0] for r in res])
check()
