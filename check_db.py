import psycopg
import os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()

cycle_id = "canary_v3_68997d43"

cur.execute("SELECT status, error_message FROM system_commands WHERE id = %s;", (cycle_id,))
print("System Command:", cur.fetchone())

cur.execute("SELECT phase, progress, error FROM pipeline_state WHERE cycle_id = %s;", (cycle_id,))
print("Pipeline State:", cur.fetchone())

cur.execute("SELECT phase, ticker, error_type, error_message FROM execution_errors WHERE cycle_id = %s;", (cycle_id,))
print("Execution Errors:", cur.fetchall())

conn.close()
