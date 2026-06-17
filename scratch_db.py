import psycopg
import sys

try:
    conn = psycopg.connect(
        host="10.0.0.16",
        port=5433,
        user="trader",
        password="trading_bot_pass",
        dbname="trading_bot"
    )
    cur = conn.cursor()
    cur.execute("SELECT error, stack_trace FROM cycle_error_log WHERE event_type = 'analysis_crash' ORDER BY timestamp DESC LIMIT 3;")
    rows = cur.fetchall()
    for row in rows:
        print("ERROR:", row[0])
        print("STACK TRACE:", row[1])
        print("-" * 50)
    conn.close()
except Exception as e:
    print("Failed:", e)
