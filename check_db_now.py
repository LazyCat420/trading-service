import sys
sys.path.insert(0, '/app')
from app.config import settings
import psycopg
conn = psycopg.connect(settings.DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT status, cycle_id, started_at FROM pipeline_state WHERE singleton_id = 'current';")
row = cur.fetchone()
print(f"CURRENT DB STATE: {row}")
