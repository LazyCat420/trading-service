import psycopg
from collections import defaultdict
conn = psycopg.connect("postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot")
cur = conn.cursor()
cur.execute("SELECT DISTINCT date FROM price_history ORDER BY date DESC LIMIT 30;")
dates = [r[0] for r in cur.fetchall()]
dates.sort()
min_d = dates[0]
max_d = dates[-1]
query = """
    SELECT tm.ticker, ph.date
    FROM ticker_metadata tm
    JOIN price_history ph ON tm.ticker = ph.ticker
    WHERE tm.sp500 = TRUE AND tm.market_cap IS NOT NULL
      AND ph.date >= %s AND ph.date <= %s
"""
cur.execute(query, (min_d, max_d))
rows = cur.fetchall()
counts = defaultdict(int)
for r in rows:
    counts[r[1]] += 1
for d in dates:
    print(f"{d}: {counts[d]}")
