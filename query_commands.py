import psycopg
conn = psycopg.connect("postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot")
res = conn.execute("SELECT command_type, payload, status FROM system_commands ORDER BY created_at DESC LIMIT 5").fetchall()
for r in res:
    print(r)
