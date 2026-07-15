import os
import psycopg
import json

conn = psycopg.connect(os.environ["DATABASE_URL"])

print("--- PIPELINE STATE ---")
row = conn.execute("SELECT * FROM pipeline_state WHERE singleton_id = 'current'").fetchone()
if row:
    print(row)

print("\n--- LAST 30 PIPELINE EVENTS ---")
res = conn.execute("SELECT timestamp, phase, step, status, detail FROM pipeline_events ORDER BY timestamp DESC LIMIT 30").fetchall()
for r in res:
    print(f"{r[0]} | Phase: {r[1]} | Step: {r[2]} | Status: {r[3]} | Detail: {r[4][:100]}")
