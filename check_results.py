from app.db.connection import get_db

with get_db() as db:
    tables = db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'").fetchall()
    print("Tables:", [t[0] for t in tables if 'result' in t[0] or 'analysis' in t[0]])
    
    # Check analysis_results
    cols = db.execute("SELECT * FROM analysis_results LIMIT 0").description
    print("analysis_results columns:", [d[0] for d in cols])
