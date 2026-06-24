import psycopg, os, json
from dotenv import load_dotenv
load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT id, result FROM v3_system_commands WHERE id = 'canary_v3_af96a09b'")
print(cur.fetchone())
