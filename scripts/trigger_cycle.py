#!/usr/bin/env python3
"""
Trigger Trading Cycle
====================

Inserts a 'START_CYCLE' command onto the `v3_system_commands` queue in MONGO —
the same queue the UI's Start Cycle button, the scheduler, the Watch Desk and
the research governor all enqueue onto, and the only one `cycle_main.py`'s
poller reads.

**It used to INSERT into Postgres.** After the cutover the poller reads Mongo
only, so the Postgres insert succeeded, printed "Successfully queued", and
started nothing — a trigger that reports success and does nothing is worse than
one that errors. Same for the watchlist lookup below: the Postgres `watchlist`
table is a frozen archive, so an empty `--tickers` run would have selected from
stale rows.

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

from app.db import mongo_query, mongo_store  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description="Trigger a trading cycle via the v3_system_commands queue")
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
    try:
        rows = mongo_query.find_rows("watchlist", {"status": "active"}, ["ticker"])
        return [r[0] for r in rows if r and r[0]]
    except Exception as e:
        print(f"Error querying active watchlist: {e}", file=sys.stderr)
        return []


def check_active_commands():
    try:
        rows = mongo_query.find_rows(
            "v3_system_commands", {"status": {"$in": ["pending", "running"]}},
            ["id", "command_type", "status", "created_at"],
            sort=[("created_at", -1)],
        )
        if rows:
            print("⚠️  Warning: The following commands are already pending or running:")
            for r in rows:
                print(f"  - [{str(r[2]).upper()}] Command ID: {r[0]}, Type: {r[1]}, Created: {r[3]}")
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
            print("❌ Error: No active tickers found in the watchlist.", file=sys.stderr)
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

    # Enqueue onto the Mongo queue, with the field set the poller claims on
    # (`status`/`created_at`) and the producers write.
    try:
        mongo_store.insert_docs("v3_system_commands", [{
            "id": cmd_id,
            "command_type": "START_CYCLE",
            "payload": json.dumps(payload),
            "status": "pending",
            "progress": 0,
            "created_at": datetime.now(timezone.utc),
        }])
        print("\n✅ Successfully queued START_CYCLE command!")
        print(f"  Command ID: {cmd_id}")
        print(f"  Status: pending")
        print("The cycle_main.py system commands poller will pick this up automatically.")
    except Exception as e:
        print(f"❌ Error inserting system command: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
