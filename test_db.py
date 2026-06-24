import psycopg
import os
from dotenv import load_dotenv

load_dotenv()
conn = psycopg.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("SELECT phase, error FROM pipeline_state WHERE cycle_id = 'cycle-v3-1782261374'")
print("Pipeline state:", cur.fetchall())

cur.execute("SELECT error_type, error_message, stack_trace FROM execution_errors WHERE cycle_id = 'cycle-v3-1782261374'")
print("Execution errors:", cur.fetchall())
