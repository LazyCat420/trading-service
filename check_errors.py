import sys
sys.path.insert(0, '/app')
from app.config import settings
import psycopg
conn = psycopg.connect(settings.DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT timestamp, phase, step, detail FROM pipeline_events WHERE status IN ('error', 'critical', 'failed') ORDER BY timestamp DESC LIMIT 5;")
rows = cur.fetchall()
for r in rows:
    print(r)
