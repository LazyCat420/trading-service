import psycopg, os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT status, result FROM system_commands WHERE id = 'canary_b5e599ff'")
print(cur.fetchone())
