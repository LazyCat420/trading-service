import logging
import sys
from app.db.connection import get_db

logging.basicConfig(level=logging.INFO)

def main():
    with get_db() as db:
        try:
            # Check execution_errors table
            db.execute(
                "SELECT cycle_id, phase, ticker, error_type, error_message, stack_trace, created_at "
                "FROM execution_errors "
                "WHERE ticker = 'BCE' OR error_message LIKE '%BCE%' "
                "ORDER BY created_at DESC LIMIT 10"
            )
            res = db.fetchall()
            print("--- EXECUTION ERRORS ---")
            for r in res:
                print(r)

            # Check cycle_audit_log table
            db.execute(
                "SELECT cycle_id, timestamp, event_type, phase, ticker, severity, message, data "
                "FROM cycle_audit_log "
                "WHERE ticker = 'BCE' OR message LIKE '%BCE%' "
                "ORDER BY timestamp DESC LIMIT 10"
            )
            res2 = db.fetchall()
            print("\n--- CYCLE AUDIT LOG ---")
            for r in res2:
                print(r)

            # Check analysis_results table
            db.execute(
                "SELECT id, cycle_id, ticker, agent_name, confidence, created_at "
                "FROM analysis_results "
                "WHERE ticker = 'BCE' "
                "ORDER BY created_at DESC LIMIT 10"
            )
            res3 = db.fetchall()
            print("\n--- ANALYSIS RESULTS ---")
            for r in res3:
                print(r)

        except Exception as e:
            print("DB Error:", e)

if __name__ == '__main__':
    main()
