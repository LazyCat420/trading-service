import sys
sys.path.insert(0, '/app')
from app.config import settings
import psycopg
conn = psycopg.connect(settings.DATABASE_URL)
cur = conn.cursor()
cur.execute("UPDATE pipeline_state SET status = 'idle', cycle_id = 'cycle-idle' WHERE singleton_id = 'current';")
conn.commit()
print("Forced DB to idle.")
