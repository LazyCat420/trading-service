import psycopg
try:
    conn = psycopg.connect("postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot")
    res = conn.execute("SELECT singleton_id, status, cycle_id, started_at, finished_at, error, phase FROM pipeline_state").fetchall()
    print("pipeline_state rows:")
    for r in res:
        print(r)
except Exception as e:
    print("Failed to query DB:", e)
