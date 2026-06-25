import os
import psycopg
from dotenv import load_dotenv
load_dotenv()
with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT agent_name, count(*), max(called_at) FROM tool_usage_stats WHERE cycle_id = 'canary_test_direct_006' GROUP BY agent_name ORDER BY max(called_at) DESC")
        for row in cur.fetchall():
            print(row)
