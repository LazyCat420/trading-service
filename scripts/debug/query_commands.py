import os
import psycopg
conn = psycopg.connect(os.environ["DATABASE_URL"])
res = conn.execute("SELECT command_type, payload, status FROM system_commands ORDER BY created_at DESC LIMIT 5").fetchall()
for r in res:
    print(r)
