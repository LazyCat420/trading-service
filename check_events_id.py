import sys
sys.path.insert(0, '/app')
from app.config import settings
import psycopg
conn = psycopg.connect(settings.DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT timestamp, cycle_id, phase, step FROM pipeline_events ORDER BY timestamp DESC LIMIT 5;")
rows = cur.fetchall()
for r in rows:
    print(r)
