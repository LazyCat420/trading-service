"""Report leaked `idle in transaction` sessions on the Postgres archive.

Advisory, never fatal — but no longer SILENT about being unable to check.

Kept as a FILE rather than a heredoc inside the hook: the hook consumes stdin
to read its JSON payload, so a `python - <<'PY'` heredoc in the same script
receives nothing and silently runs an empty program. That is exactly how both
of these hooks no-opped through four test runs before a `bash -x` trace showed
it.

WHY IT STILL EXISTS AFTER THE MONGO CUTOVER
-------------------------------------------
The trading cycle left Postgres on 2026-08-19, but the SERVER did not go away:
`trading_bot` still hosts treesearch-service's 14 live tables, and a leaked
idle-in-transaction session still holds locks that hang somebody else's query.
The 2026-08-30 audit found three such sessions idle in ROLLBACK since 08-26.

WHAT CHANGED 2026-08-30
-----------------------
1. It reads `PG_ARCHIVE_URL` before `DATABASE_URL`. The archive DSN is being
   moved out of the ambient environment precisely so nothing picks it up by
   accident; this hook is one of the few things that should still have it, and
   it should say so by name.
2. `except Exception: sys.exit(0)` used to swallow "no DSN", "no driver" and "a
   real error" into the same silence. A diagnostic that cannot run must say it
   cannot run — otherwise the day the DSN moves, this stops checking and looks
   exactly like a clean database. It still never fails the turn.
"""
import os
import sys


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # .env.migration first: it is where the archive DSN is heading.
    for name in (".env.migration", ".env"):
        if os.path.exists(name):
            load_dotenv(name)


def main() -> None:
    _load_env()
    url = os.environ.get("PG_ARCHIVE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("· idle-txn check skipped: no PG_ARCHIVE_URL/DATABASE_URL in env")
        return
    try:
        import psycopg2
    except ImportError:
        print("· idle-txn check skipped: psycopg2 not installed")
        return

    url = url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = psycopg2.connect(url, connect_timeout=6)
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
    except Exception as e:  # noqa: BLE001 - advisory only, but name the failure
        print(f"· idle-txn check could not run: {type(e).__name__}: {e}")
        return

    if rows:
        print(
            "\n⚠ LEAKED DB SESSIONS (idle in transaction) — these hold locks "
            "and will hang the NEXT query, not this one:"
        )
        for pid, q, age in rows:
            print(f"   pid={pid} idle {age}s :: {q}")
        print("   clear with: SELECT pg_terminate_backend(<pid>);")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)  # a diagnostic must never break the turn
