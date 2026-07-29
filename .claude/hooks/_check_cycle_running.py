"""Print `running|<cycle_id>` when a V3 cycle is in flight, else `idle|`.

A FILE, not a heredoc: the calling hook consumes stdin for its JSON payload,
so an inline `python - <<'PY'` heredoc receives nothing and runs an empty
program — silently. Fails OPEN (prints `unknown|`) so a DB blip never blocks a
deploy.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
    import psycopg2

    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=8)
    cur = conn.cursor()
    cur.execute(
        "SELECT status, cycle_id FROM pipeline_state WHERE singleton_id = 'current'"
    )
    row = cur.fetchone()
    conn.close()
    print(f"{row[0]}|{row[1]}" if row else "unknown|")
except Exception:
    print("unknown|")
