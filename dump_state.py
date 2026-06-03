import sys
sys.path.insert(0, '/app')
from app.config import settings
import psycopg
conn = psycopg.connect(settings.DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT * FROM pipeline_state WHERE singleton_id = 'current';")
row = cur.fetchone()
col_names = [desc[0] for desc in cur.description]
print(dict(zip(col_names, row)))
