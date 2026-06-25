import sys
from app.db.connection import get_db
with get_db() as db:
    tables = db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()
    print([t[0] for t in tables])
