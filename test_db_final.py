def check():
    from app.db.connection import get_db
    with get_db() as db:
        try:
            print(db.execute("SELECT * FROM pipeline_state WHERE singleton_id='current'").fetchone())
        except Exception as e:
            print("ERROR", e)
check()
