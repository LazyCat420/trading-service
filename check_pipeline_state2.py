import psycopg, os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT cycle_id, status, progress, EXTRACT(EPOCH FROM (NOW() - updated_at)) as seconds_ago FROM pipeline_state WHERE singleton_id = 'current'")
print("Pipeline state:", cur.fetchone())
