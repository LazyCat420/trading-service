#!/usr/bin/env python3
import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.connection import get_db
from app.utils.politician_matcher import resolve_bioguide_id

def main():
    print("Starting Congress Trades backfill for bioguide_id...")
    
    with get_db() as db:
        # Fetch all trades
        trades = db.execute("SELECT id, politician FROM congress_trades").fetchall()
        print(f"Loaded {len(trades)} trades from database.")
        
        mapped_count = 0
        unmapped_count = 0
        
        # We process and execute updates
        with db.transaction():
            for trade_id, politician in trades:
                bio_id = resolve_bioguide_id(db, politician)
                if bio_id:
                    db.execute(
                        "UPDATE congress_trades SET bioguide_id = %s WHERE id = %s",
                        [bio_id, trade_id]
                    )
                    mapped_count += 1
                else:
                    unmapped_count += 1
                    
        print(f"Backfill finished: {mapped_count} mapped, {unmapped_count} unmapped.")

if __name__ == "__main__":
    main()
