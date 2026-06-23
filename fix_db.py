import sys
import logging
from app.db.connection import get_db

with get_db() as db:
    # 1. Mark stuck cycles as aborted
    db.execute("UPDATE cycles SET status = 'aborted', error_message = 'Container was restarted during deployment' WHERE status IN ('starting', 'collecting', 'analyzing', 'trading')")
    
    # 2. Pause all tickers except AAPL
    db.execute("UPDATE watchlist SET status = 'paused', status_reason = 'Paused for 1-stock test' WHERE status = 'active' AND ticker != 'AAPL'")
    
    # 3. Ensure AAPL is active
    db.execute("INSERT INTO watchlist (ticker, status, source) VALUES ('AAPL', 'active', 'manual') ON CONFLICT (ticker) DO UPDATE SET status = 'active', status_reason = NULL")
    
    print("Database fixed. Stuck cycles aborted, watchlist reduced to just AAPL.")
