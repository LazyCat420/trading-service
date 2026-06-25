import psycopg2
from datetime import datetime

url = "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot"
try:
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    
    def q(title, sql):
        print(f"\n--- {title} ---")
        try:
            cur.execute(sql)
            if cur.description:
                for row in cur.fetchall():
                    print(row)
            else:
                print("Executed.")
        except Exception as e:
            print(f"Error: {e}")
            conn.rollback()

    q("0A.1 Tables", "SELECT table_name FROM information_schema.tables WHERE table_schema='public';")
    q("0A.2 ticker_reports", "SELECT COUNT(*), MAX(created_at) FROM ticker_reports;")
    q("0A.3 shared_desk", "SELECT COUNT(*), MAX(created_at) FROM shared_desk;")
    q("0A.4 shared_desk phases", "SELECT phase, COUNT(*) FROM shared_desk GROUP BY phase ORDER BY 1;")
    q("0A.5 shared_desk recent", "SELECT ticker, phase, created_at FROM shared_desk ORDER BY created_at DESC LIMIT 20;")
    q("0A.6 shared_desk PM_DONE keys", "SELECT jsonb_object_keys(desk_data) FROM shared_desk WHERE phase = 'PM_DONE' ORDER BY created_at DESC LIMIT 1;")
    q("0A.7 analysis_results", "SELECT COUNT(*), MAX(created_at) FROM analysis_results;")
    q("0A.8 analysis_results recent", "SELECT ticker, (result_json::jsonb)->>'action' AS action, confidence, created_at FROM analysis_results ORDER BY created_at DESC LIMIT 10;")

    # 0B
    q("0B.1 pipeline_state current", "SELECT singleton_id, status, cycle_id, started_at, finished_at, error FROM pipeline_state WHERE singleton_id = 'current';")
    q("0B.2 pipeline_events for current", "SELECT event_type, agent_name, phase, created_at FROM pipeline_events WHERE cycle_id = (SELECT cycle_id FROM pipeline_state WHERE singleton_id = 'current') ORDER BY created_at ASC;")
    q("0B.4 pipeline_events recent count", "SELECT COUNT(*) FROM pipeline_events WHERE created_at > NOW() - INTERVAL '48 hours';")

    conn.close()
except Exception as e:
    print(e)
