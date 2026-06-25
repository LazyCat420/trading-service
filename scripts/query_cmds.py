import psycopg, os
from dotenv import load_dotenv
load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT id, command_type, status, created_at, started_at, completed_at FROM v3_system_commands ORDER BY created_at DESC LIMIT 5")
print("V3 Commands:")
for row in cur.fetchall():
    print(row)
cur.execute("SELECT id, command_type, status, created_at FROM system_commands ORDER BY created_at DESC LIMIT 5")
print("V1 Commands:")
for row in cur.fetchall():
    print(row)
