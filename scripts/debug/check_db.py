import sys
sys.path.insert(0, '/app')
from app.config import settings
import psycopg
conn = psycopg.connect(settings.DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT singleton_id, cycle_id, status FROM pipeline_state;")
rows = cur.fetchall()
print("ROWS IN PIPELINE_STATE:")
for r in rows:
    print(r)
