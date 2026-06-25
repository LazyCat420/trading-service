import psycopg
from psycopg.rows import dict_row
from pathlib import Path
import json

DATABASE_URL = "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot"

def run_audit():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # Get latest cycle_id
            cur.execute("SELECT cycle_id FROM pipeline_state ORDER BY started_at DESC LIMIT 1;")
            row = cur.fetchone()
            if not row:
                print("No pipeline_state found.")
                return
            cycle_id = row['cycle_id']
            print(f"Latest cycle_id: {cycle_id}")

            # 1. DB: Total Analysis Results
            print("\\n--- DB: Total Analysis Results ---")
            cur.execute("SELECT COUNT(*) as total FROM analysis_results WHERE cycle_id = %s;", (cycle_id,))
            print(cur.fetchone())

            # 2. DB: Fallback HOLDs
            print("\\n--- DB: Fallback HOLDs (0% Confidence) ---")
            cur.execute("SELECT COUNT(*) as fallbacks FROM analysis_results WHERE cycle_id = %s AND thesis_verdict = 'HOLD' AND confidence = 0;", (cycle_id,))
            print(cur.fetchone())

            # 3. DB: Brain Graph Nodes
            print("\\n--- DB: Brain Graph Nodes Created ---")
            cur.execute("SELECT COUNT(*) as nodes FROM ontology_nodes WHERE source_cycle_id = %s;", (cycle_id,))
            print(cur.fetchone())

            # 4. DB: Brain Graph Edges
            print("\\n--- DB: Brain Graph Edges Created ---")
            cur.execute("SELECT COUNT(*) as edges FROM ontology_edges WHERE source_cycle_id = %s;", (cycle_id,))
            print(cur.fetchone())

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
