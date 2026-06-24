import psycopg, os
from dotenv import load_dotenv
load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT phase, desk_data FROM shared_desk WHERE cycle_id = 'canary_v3_0393553b'")
row = cur.fetchone()
if row:
    print(f"Phase: {row[0]}")
else:
    print("No row found yet")
