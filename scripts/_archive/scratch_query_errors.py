import sys
import os
sys.path.insert(0, '/home/lazycat/github/projects/sun/trading-service')

from app.db import connection

try:
    with connection.get_db() as db:
        res = db.execute("SELECT cycle_id, timestamp, component, ticker, error_type, substring(error_message from 1 for 150) FROM execution_errors ORDER BY timestamp DESC LIMIT 20").fetchall()
        for r in res:
            print(f"CYCLE: {r[0]}, TIME: {r[1]}, COMP: {r[2]}, TICKER: {r[3]}, TYPE: {r[4]}, MSG: {r[5]}")
except Exception as e:
    print("Failed:", e)
