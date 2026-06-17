import sys
import os
sys.path.insert(0, '/home/lazycat/github/projects/sun/trading-service')

from app.db import connection

try:
    with connection.get_db() as db:
        res = db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()
        for r in res:
            print(r[0])
except Exception as e:
    print("Failed:", e)
