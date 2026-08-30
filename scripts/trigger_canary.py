"""Trigger one canary cycle and watch it to completion.

Converted off Postgres 2026-08-30. It used to INSERT straight into
`v3_system_commands` on the frozen archive, so the command landed in a store
`cycle_main.poll_system_commands` does not drain — the canary could never fire
and would have sat printing "Phase: ..." against a desk that never appeared.
The enqueue now goes through `app.services.cycle_queue`, the one writer, for
exactly the reason its docstring gives.
"""
import json
import time
import uuid

from app.db import mongo_query, mongo_store
from app.services.cycle_queue import enqueue_start_cycle


def trigger_canary():
    cycle_id = f"canary_v3_{uuid.uuid4().hex[:8]}"
    print(f"Triggering manual canary cycle: {cycle_id}")

    payload = {
        "trade": False,
        "analyze": True,
        "collect": False,
        "tickers": ["AAPL"],
        "max_tickers": 1,
        "start_fresh": True,
        "cycle_id": cycle_id,
    }
    cmd_id = enqueue_start_cycle(payload, prefix="canary")
    print(f"Queued {cmd_id} on v3_system_commands. Waiting for SharedDesk phase updates...")

    for _ in range(120):  # 10 minutes max
        time.sleep(5)
        row = mongo_query.find_row(
            "shared_desk", {"cycle_id": cycle_id}, ["phase", "desk_data"]
        )
        if not row:
            continue

        phase, desk_data_str = row
        desk_data = (
            desk_data_str if isinstance(desk_data_str, dict)
            else json.loads(desk_data_str) if desk_data_str else {}
        )

        bull = desk_data.get("bull_argument")
        bear = desk_data.get("bear_rebuttal")
        regime = desk_data.get("regime_classification")
        decision = desk_data.get("final_decision")
        outcomes = desk_data.get("phase_outcomes", {})

        print(f"Phase: {phase}")
        if phase in ("PM_DONE", "ABORTED"):
            print("Cycle Finished.")
            print(f"Bull: {bull is not None}, Bear: {bear is not None}, Regime: {regime is not None}")
            print(f"Decision: {decision}")
            print(f"Outcomes: {outcomes}")

            # Wait for pipeline_state to be 'done' so the next cycle cannot race this one.
            print("Waiting for PipelineService to finalize 'done' state...")
            for _ in range(30):
                time.sleep(2)
                state = mongo_query.find_row(
                    "pipeline_state", {"singleton_id": "current"}, ["status"]
                )
                if state and state[0] == "done":
                    print("Pipeline finalized.")
                    break
            break


if __name__ == "__main__":
    trigger_canary()
