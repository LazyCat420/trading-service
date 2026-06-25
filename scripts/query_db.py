import asyncio
from app.db.connection import get_db

with get_db() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT tool_name, error_message, called_at FROM tool_usage_stats ORDER BY called_at DESC LIMIT 20;")
        print("--- Recent Tool Calls ---")
        for row in cur.fetchall():
            print(row)
        
        cur.execute("SELECT model, messages, response, created_at FROM llm_tracker ORDER BY created_at DESC LIMIT 5;")
        print("\n--- Recent LLM Calls ---")
        for row in cur.fetchall():
            print(row[0], row[3])
            
        cur.execute("SELECT cycle_id, status, updated_at FROM cycles ORDER BY updated_at DESC LIMIT 5;")
        print("\n--- Recent Cycles ---")
        for row in cur.fetchall():
            print(row)
