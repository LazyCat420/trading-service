import sys
sys.path.insert(0, '/app')
from app.config import settings
import psycopg
conn = psycopg.connect(settings.DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT command_type, status, created_at, payload FROM system_commands ORDER BY created_at DESC LIMIT 5;")
for row in cur.fetchall():
    print(row)
