import psycopg, os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT phase FROM shared_desk WHERE cycle_id = 'cycle-v3-1782261374'")
print("Shared desk:", cur.fetchone())
