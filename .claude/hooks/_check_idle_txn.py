"""Report leaked `idle in transaction` sessions. Advisory, never fatal.

Kept as a FILE rather than a heredoc inside the hook: the hook consumes stdin
to read its JSON payload, so a `python - <<'PY'` heredoc in the same script
receives nothing and silently runs an empty program. That is exactly how both
of these hooks no-opped through four test runs before a `bash -x` trace showed
it.
"""
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
    import psycopg2

    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=6)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT pid, left(query, 70), extract(epoch from now() - state_change)::int
        FROM pg_stat_activity
        WHERE state = 'idle in transaction'
          AND state_change < now() - interval '30 seconds'
        ORDER BY state_change LIMIT 3
        """
    )
    rows = cur.fetchall()
    conn.close()
    if rows:
        print(
            "\n⚠ LEAKED DB SESSIONS (idle in transaction) — these hold locks "
            "and will hang the NEXT query, not this one:"
        )
        for pid, q, age in rows:
            print(f"   pid={pid} idle {age}s :: {q}")
        print("   clear with: SELECT pg_terminate_backend(<pid>);")
except Exception:
    sys.exit(0)   # fail silent: a diagnostic must never break the turn
