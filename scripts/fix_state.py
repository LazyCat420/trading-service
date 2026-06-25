import sys
from app.db.connection import get_db
with get_db() as db:
    db.execute("UPDATE pipeline_state SET status = 'aborted', progress = 'Cycle aborted during deployment' WHERE status NOT IN ('aborted', 'done', 'error')")
    db.execute("UPDATE watchlist SET status = 'paused', status_reason = 'Paused for 1-stock test' WHERE status = 'active' AND ticker != 'AAPL'")
    db.execute("INSERT INTO watchlist (ticker, status, source) VALUES ('AAPL', 'active', 'manual') ON CONFLICT (ticker) DO UPDATE SET status = 'active', status_reason = NULL")
    print("Database fixed!")
