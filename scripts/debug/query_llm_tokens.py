import os
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]

def query_llm_tokens():
    try:
        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT created_at, ticker, agent_step, endpoint_name, prompt_tokens, completion_tokens, execution_ms 
            FROM llm_audit_logs 
            WHERE cycle_id = 'cycle-1780050590' AND agent_step LIKE '%selector%' 
            ORDER BY created_at ASC;
        """)
        
        rows = cur.fetchall()
        print(f"\nFound {len(rows)} selector token details:")
        print("-" * 120)
        for row in rows:
            created, ticker, agent_step, ep_name, p_tok, c_tok, ms = row
            print(f"[{created}] {ticker} | {agent_step} | {ep_name} | prompt={p_tok}, comp={c_tok}, total={p_tok + (c_tok or 0)} | execution={ms}ms")
            
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    query_llm_tokens()
