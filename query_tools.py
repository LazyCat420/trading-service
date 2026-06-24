import psycopg, os
from dotenv import load_dotenv
load_dotenv()
conn = psycopg.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute("SELECT tool_name, success, execution_ms, error_message, called_at FROM tool_usage_stats WHERE cycle_id = 'cycle-v3-1782266266' AND agent_name = 'v3_junior_analyst' ORDER BY called_at ASC;")
tools = cur.fetchall()
print(f'Total tools called: {len(tools)}')
for t in tools:
    print(t)
