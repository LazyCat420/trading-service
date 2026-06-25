import sys
import os
sys.path.insert(0, os.path.abspath("."))
from app.db.connection import get_db

with get_db() as db:
    rows = db.execute("SELECT rule_category, rule_text FROM trading_constitution LIMIT 10").fetchall()
    print("trading_constitution rows:", len(rows))
    for r in rows:
        print(f" - {r[0]}: {r[1][:50]}...")
