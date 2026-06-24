import psycopg, os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT id, status, result::text, error_message FROM system_commands WHERE id = 'canary_63a625a8'")
print(cur.fetchall())
