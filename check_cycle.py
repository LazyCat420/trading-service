import psycopg

DATABASE_URL = "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot"

with psycopg.connect(DATABASE_URL) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM pipeline_state LIMIT 1;")
        print("Pipeline State:")
        colnames = [desc[0] for desc in cur.description]
        row = cur.fetchone()
        if row:
            for c, v in zip(colnames, row):
                print(f"  {c}: {v}")
