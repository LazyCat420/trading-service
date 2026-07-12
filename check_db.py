from app.db.connection import get_db

def check_db():
    try:
        with get_db() as db:
            db.execute("SELECT COUNT(*) FROM tool_usage_stats")
            print("tool_usage_stats total:", db.fetchone()[0])
            
            db.execute("SELECT COUNT(*) FROM tool_usage_stats WHERE called_at > NOW() - INTERVAL '24 hours'")
            print("tool_usage_stats recent:", db.fetchone()[0])
            
            db.execute("SELECT COUNT(*) FROM agent_tool_telemetry")
            print("agent_tool_telemetry total:", db.fetchone()[0])
            
            db.execute("SELECT COUNT(*) FROM agent_tool_telemetry WHERE created_at > NOW() - INTERVAL '24 hours'")
            print("agent_tool_telemetry recent:", db.fetchone()[0])
            
            # Test the query from trading-client tools.py
            db.execute("""
                 WITH combined_stats AS (
                    SELECT 
                        tool_name, 
                        agent_name, 
                        success, 
                        execution_ms, 
                        called_at, 
                        service_source
                    FROM tool_usage_stats
                    UNION ALL
                    SELECT 
                        tool_name, 
                        agent_name, 
                        success, 
                        elapsed_ms AS execution_ms, 
                        created_at AS called_at, 
                        'trading-service' AS service_source
                    FROM agent_tool_telemetry
                 )
                 SELECT count(*) FROM combined_stats WHERE called_at > NOW() - INTERVAL '24 hours'
            """)
            print("combined recent:", db.fetchone()[0])
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    check_db()
