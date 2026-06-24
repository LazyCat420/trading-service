import psycopg, os
from dotenv import load_dotenv
load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("""
    SELECT application_name, client_addr, count(*) 
    FROM pg_stat_activity 
    WHERE datname = 'trading_bot' 
    GROUP BY application_name, client_addr
""")
for row in cur.fetchall():
    print(row)
