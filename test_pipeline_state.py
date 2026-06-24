import psycopg, os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT status, phase, progress, error, cycle_id FROM pipeline_state WHERE singleton_id = 'current'")
print("Current pipeline state:", cur.fetchone())
