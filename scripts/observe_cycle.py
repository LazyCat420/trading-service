#!/usr/bin/env python3
"""Run one V3 cycle in the DEPLOYED container, with trade execution disabled.

Verification harness for the 2026-07-24 agent audit.

Enqueues a START_V3_CYCLE onto `v3_system_commands` — the same queue the UI's
"Start Cycle" button and the scheduler use — and waits for the cycle-backend
container to pick it up.

Do NOT go back to calling `PipelineService.start_cycle()` in-process. That does
run a real cycle and writes real desks, but:

  * it executes THIS checkout's code, not the deployed image, so it verifies
    the wrong artifact;
  * `emit` events are in-process, so the frontend's Live Event Logger stays
    empty and the Run Cycle widget reads IDLE while the Market Data panel
    (which reads pipeline_state from the DB) shows ANALYZING — a split-brain
    that looks like a bug in the app.

`trade=False` means decisions are produced and saved but no orders are placed.

    python scripts/observe_cycle.py --tickers JPM,NVDA,MP
    python scripts/observe_cycle.py --tickers JPM --trade    # actually trades
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POLL_SECONDS = 10
DEFAULT_TIMEOUT = 2400


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="JPM,NVDA,MP")
    ap.add_argument("--trade", action="store_true",
                    help="ACTUALLY place trades (default: observation only)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--no-wait", action="store_true",
                    help="enqueue and exit without waiting")
    args = ap.parse_args()

    from scripts.migration.pg_connection import get_db

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    cycle_id = f"cycle-observe-{int(time.time())}"
    payload = {
        "tickers": tickers,
        "cycle_id": cycle_id,
        "collect": True,
        "analyze": True,
        "trade": bool(args.trade),
    }

    # Refuse to queue behind a cycle already in flight — two concurrent cycles
    # fight over the pipeline_state singleton.
    with get_db() as db:
        busy = db.execute(
            "SELECT id, command_type, status FROM v3_system_commands "
            "WHERE status IN ('pending', 'running') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if busy:
            print(f"REFUSING: a command is already {busy[2]} ({busy[1]}, id={busy[0]}). "
                  f"Wait for it or clear it first.")
            return 1

        cmd_id = f"obs-{uuid.uuid4().hex[:8]}"
        db.execute(
            "INSERT INTO v3_system_commands (id, command_type, payload) VALUES (%s, %s, %s)",
            [cmd_id, "START_V3_CYCLE", json.dumps(payload)],
        )

    print(f"enqueued START_V3_CYCLE {cmd_id}")
    print(f"  cycle_id : {cycle_id}")
    print(f"  tickers  : {', '.join(tickers)}")
    print(f"  trading  : {'ENABLED' if args.trade else 'DISABLED (observation only)'}")
    print("  -> runs in the deployed container; watch it in the frontend")

    if args.no_wait:
        print(f"\nCYCLE_ID={cycle_id}")
        return 0

    deadline = time.monotonic() + args.timeout
    last = None
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        with get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM v3_system_commands WHERE id = %s",
                [cmd_id],
            ).fetchone()
            state = db.execute(
                "SELECT status, phase, progress FROM pipeline_state"
            ).fetchone()
        status = row[0] if row else "?"
        if state and state != last:
            print(f"  [{status}] pipeline={state[0]} phase={state[1]} {state[2] or ''}")
            last = state
        if status in ("completed", "error", "skipped"):
            print(f"\ncommand {status}" + (f": {row[1]}" if row[1] else ""))
            print(f"CYCLE_ID={cycle_id}")
            return 0 if status == "completed" else 1

    print(f"\nTIMED OUT after {args.timeout}s — cycle may still be running")
    print(f"CYCLE_ID={cycle_id}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
