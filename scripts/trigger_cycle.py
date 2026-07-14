#!/usr/bin/env python3
"""
Trigger Trading Cycle
====================

Inserts a 'START_CYCLE' system command into the database system_commands queue.
The background poller inside cycle_main.py will pick it up and execute the run.

Usage:
    python scripts/trigger_cycle.py --tickers BCE,UBS,AMP
    python scripts/trigger_cycle.py --no-trade --tickers AAPL
    python scripts/trigger_cycle.py (uses active watchlist tickers)
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.connection import get_db

def parse_args():
    parser = argparse.ArgumentParser(description="Trigger a trading cycle via system_commands database queue")
    parser.add_argument("--tickers", "-t", type=str, default="", help="Comma-separated tickers (e.g. BCE,UBS,AMP). If empty, queries active watchlist.")
    parser.add_argument("--collect", action="store_true", default=True, help="Run collection phase (default: True)")
    parser.add_argument("--no-collect", dest="collect", action="store_false", help="Skip collection phase")
    parser.add_argument("--analyze", action="store_true", default=True, help="Run analysis phase (default: True)")
    parser.add_argument("--no-analyze", dest="analyze", action="store_false", help="Skip analysis phase")
    parser.add_argument("--trade", action="store_true", default=True, help="Run trading/execution phase (default: True)")
    parser.add_argument("--no-trade", dest="trade", action="store_false", help="Skip trading/execution phase")
    parser.add_argument("--max-tickers", type=int, default=None, help="Overriding max tickers limit")
    return parser.parse_args()

def get_active_watchlist_tickers():
    tickers = []
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT ticker FROM watchlist WHERE status = 'active'"
            ).fetchall()
            if rows:
                tickers = [row[0] for row in rows]
    except Exception as e:
        print(f"Error querying active watchlist: {e}", file=sys.stderr)
    return tickers

def check_active_commands():
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT id, command_type, status, created_at FROM v3_system_commands WHERE status IN ('pending', 'running') ORDER BY created_at DESC"
            ).fetchall()
            if rows:
                print("⚠️  Warning: The following commands are already pending or running:")
                for r in rows:
                    print(f"  - [{r[2].upper()}] Command ID: {r[0]}, Type: {r[1]}, Created: {r[3]}")
                print()
    except Exception as e:
        print(f"Error checking active commands: {e}", file=sys.stderr)

def main():
    args = parse_args()
    
    # Parse tickers
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        print("No tickers specified. Querying active watchlist tickers...")
        tickers = get_active_watchlist_tickers()
        if not tickers:
            print("❌ Error: No active tickers found in watchlist database.", file=sys.stderr)
            sys.exit(1)
            
    print(f"Target Tickers: {tickers}")
    check_active_commands()
    
    # Generate UUID and payload
    cmd_id = f"cmd-{uuid.uuid4()}"
    payload = {
        "tickers": tickers,
        "collect": args.collect,
        "analyze": args.analyze,
        "trade": args.trade,
    }
    if args.max_tickers is not None:
        payload["max_tickers"] = args.max_tickers
        
    print(f"Constructed Payload: {json.dumps(payload, indent=2)}")
    
    # Insert command into DB
    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO v3_system_commands (id, command_type, payload, status, created_at)
                VALUES (%s, 'START_CYCLE', %s, 'pending', CURRENT_TIMESTAMP)
                """,
                [cmd_id, json.dumps(payload)]
            )
        print(f"\n✅ Successfully queued START_CYCLE command!")
        print(f"  Command ID: {cmd_id}")
        print(f"  Status: pending")
        print(f"The cycle_main.py system commands poller will pick this up automatically.")
    except Exception as e:
        print(f"❌ Error inserting system command: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
