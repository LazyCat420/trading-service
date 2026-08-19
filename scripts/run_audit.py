"""Quick per-cycle audit: four counts from the store, then the local log.

Reads MongoDB — this is one of the instruments the cutover is verified WITH.
A Postgres reader pointed at a Mongo-only cycle counts an empty table and
prints a clean audit.
"""
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_query, mongo_store  # noqa: E402


def run_audit():
    # pipeline_state is a singleton row; the ORDER BY started_at DESC LIMIT 1
    # is preserved rather than assumed away, since a second row would change
    # which cycle this audits.
    row = mongo_query.find_row(
        "pipeline_state", {}, ["cycle_id"], sort=[("started_at", -1)])
    if not row:
        print("No pipeline_state found.")
        return
    cycle_id = row[0]
    print(f"Latest cycle_id: {cycle_id}")

    print("\n--- DB: Total Analysis Results ---")
    print({"total": mongo_store.count_docs("analysis_results", {"cycle_id": cycle_id})})

    print("\n--- DB: Fallback HOLDs (0% Confidence) ---")
    print({"fallbacks": mongo_store.count_docs("analysis_results", {
        "cycle_id": cycle_id, "thesis_verdict": "HOLD", "confidence": 0})})

    print("\n--- DB: Brain Graph Nodes Created ---")
    print({"nodes": mongo_store.count_docs(
        "ontology_nodes", {"source_cycle_id": cycle_id})})

    print("\n--- DB: Brain Graph Edges Created ---")
    print({"edges": mongo_store.count_docs(
        "ontology_edges", {"source_cycle_id": cycle_id})})

    # 5. Local Logs: JSONL parsing
    print("\\n--- LOGS: Cycle Events ---")
    log_path = Path(f"logs_local/cycles/{cycle_id}.jsonl")
    if not log_path.exists():
        log_path = Path(f"logs/cycles/{cycle_id}.jsonl")
    
    if not log_path.exists():
        print(f"Log file not found for {cycle_id}")
        return

    crashes = 0
    timeouts = 0
    completes = 0
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                step = entry.get("step", "")
                if step == "error_analysis_crash":
                    crashes += 1
                    print(f"CRASH: {entry.get('ticker')} - {entry.get('payload', {}).get('error', '')[:200]}")
                elif step == "error_thesis_timeout":
                    timeouts += 1
                elif step == "v2_pipeline_complete":
                    completes += 1
                    # Also find what stages completed if there were fallback HOLDs (could print all for now)
            except Exception:
                pass
    
    print(f"Log Stats: Crashes={crashes}, Timeouts={timeouts}, Completes={completes}")

if __name__ == "__main__":
    run_audit()
