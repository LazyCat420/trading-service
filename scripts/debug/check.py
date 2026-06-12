import sys
sys.path.insert(0, '/app')
from app.config import settings
import psycopg
conn = psycopg.connect(settings.DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT cycle_id, started_at, finished_at FROM pipeline_state WHERE singleton_id='current';")
print("RESULT:", cur.fetchone())
