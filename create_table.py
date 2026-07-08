from app.db.connection import get_db

sql = """
CREATE TABLE IF NOT EXISTS sec_13f_performance (
    cik            TEXT PRIMARY KEY,
    return_1y      DOUBLE PRECISION,
    return_3y_ann  DOUBLE PRECISION,
    win_rate       DOUBLE PRECISION,
    last_calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
try:
    with get_db() as db:
        db.execute(sql)
    print("Table created successfully.")
except Exception as e:
    print(f"Error: {e}")
