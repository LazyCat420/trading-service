from app.db.connection import get_db
with get_db() as db:
    cols = db.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'analysis_results'").fetchall()
    print("Columns for analysis_results:")
    for c in cols:
        print(f" - {c[0]}: {c[1]}")
