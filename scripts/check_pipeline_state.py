import psycopg, os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'pipeline_state'")
print("Pipeline state columns:", [r[0] for r in cur.fetchall()])
cur.execute("SELECT * FROM pipeline_state")
print("Pipeline state rows:", cur.fetchall())
