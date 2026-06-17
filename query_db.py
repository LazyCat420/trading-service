import sys
import os
sys.path.insert(0, '/home/lazycat/github/projects/sun/trading-service')

from app.db import connection

try:
    with connection.get_db() as db:
        res = db.execute("SELECT cycle_id, timestamp, ticker, error, stage, extra FROM cycle_error_log WHERE event_type = 'analysis_crash' ORDER BY timestamp DESC LIMIT 5").fetchall()
        for r in res:
            print(f"CYCLE: {r[0]}, TIMESTAMP: {r[1]}, TICKER: {r[2]}, ERROR: {r[3]}, STAGE: {r[4]}, EXTRA: {r[5]}")
except Exception as e:
    print("Failed:", e)
