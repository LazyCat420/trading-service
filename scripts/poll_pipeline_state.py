#!/usr/bin/env python3
"""
Poll Pipeline State for Transitions
===================================
Polls the postgres database pipeline_state table. Exits when the cycle_id,
phase, or status changes from the specified baseline values.

Usage:
    python scripts/poll_pipeline_state.py --cycle-id cycle-123 --phase collecting --status started
"""

import argparse
import os
import sys
import time

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from app.db.connection import get_db
except ImportError:
    # Fallback path inclusion
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.db.connection import get_db

def poll_state(last_cycle_id: str, last_phase: str, last_status: str, timeout_seconds: int = 900):
    start_time = time.monotonic()
    
    # Normalize inputs
    last_cycle_id = (last_cycle_id or "").strip()
    last_phase = (last_phase or "").strip()
    last_status = (last_status or "").strip()
    
    print(f"Polling database for state change from baseline: cycle_id='{last_cycle_id}', phase='{last_phase}', status='{last_status}'")
    sys.stdout.flush()
    
    while time.monotonic() - start_time < timeout_seconds:
        try:
            with get_db() as db:
                db.execute(
                    "SELECT cycle_id, phase, status FROM pipeline_state WHERE singleton_id = 'current'"
                )
                row = db.fetchone()
                if row:
                    curr_cycle_id = (row[0] or "").strip()
                    curr_phase = (row[1] or "").strip()
                    curr_status = (row[2] or "").strip()
                    
                    if (curr_cycle_id != last_cycle_id or 
                        curr_phase != last_phase or 
                        curr_status != last_status):
                        print(f"\nSTATE_CHANGED: cycle_id='{curr_cycle_id}', phase='{curr_phase}', status='{curr_status}'")
                        sys.stdout.flush()
                        sys.exit(0)
        except Exception as e:
            print(f"Error polling database: {e}", file=sys.stderr)
            sys.stderr.flush()
            
        time.sleep(10)
        
    print(f"\nPOLL_TIMEOUT: No state change detected within {timeout_seconds} seconds.")
    sys.stdout.flush()
    sys.exit(2)

def main():
    parser = argparse.ArgumentParser(description="Poll database pipeline state transitions")
    parser.add_argument("--cycle-id", default="", help="Baseline cycle ID")
    parser.add_argument("--phase", default="", help="Baseline phase")
    parser.add_argument("--status", default="", help="Baseline status")
    parser.add_argument("--timeout", type=int, default=900, help="Timeout in seconds")
    args = parser.parse_args()
    
    poll_state(args.cycle_id, args.phase, args.status, args.timeout)

if __name__ == "__main__":
    main()
