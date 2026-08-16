import psycopg, os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
conn.autocommit = True
cur.execute("UPDATE pipeline_state SET status = 'idle', cycle_id = NULL, progress = '' WHERE singleton_id = 'current'")
print("Pipeline state reset to idle.")
