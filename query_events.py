import psycopg, os
from dotenv import load_dotenv
load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("""
    SELECT timestamp, phase, step, detail 
    FROM pipeline_events 
    WHERE cycle_id = 'cycle-1782331390' 
    ORDER BY timestamp DESC 
    LIMIT 10
""")
for row in cur.fetchall():
    print(row)
