import psycopg, os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT status, result, error_message, started_at, completed_at FROM system_commands WHERE id LIKE 'canary_%'")
print("Canary commands:", cur.fetchall())
