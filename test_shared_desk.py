import psycopg, os, json
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT desk_id, cycle_id, phase FROM shared_desk ORDER BY created_at DESC LIMIT 5;")
print("Shared Desks:", cur.fetchall())
