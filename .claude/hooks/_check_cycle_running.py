"""Print `running|<cycle_id>` when a V3 cycle is in flight, else `idle|`.

Reads `pipeline_state` from MONGO. It used to read Postgres with `psycopg2`,
which was wrong twice over:

  1. the app image carries no Postgres driver at all since the 2026-08-18
     teardown, and the repo uses psycopg3 rather than psycopg2, so the import
     raised on every single call from inside the container; and

     (⚠ corrected 2026-08-30: this used to read "psycopg2 is not installed",
     full stop. `psycopg2 2.9.12` IS installed in this checkout's `.venv` and
     in the system python — it is pinned in no requirements file, which is why
     nobody noticed. The claim was true of the IMAGE and false of the BOX, and
     a hook two directories away was quietly relying on the false half.)
  2. `pipeline_state` is staged at `:mongo` in deploy-kit/.env.deploy, so the
     Postgres row stops being written from the next deploy onward.

Either alone made this print `unknown|` forever — and the calling hook
(`guard_deploy_during_cycle.sh`) only blocks on `running*`, so `unknown` fell
through to exit 0. The guard has been permitting every deploy, including ones
that would kill a live cycle, while looking installed and healthy.

A FILE, not a heredoc: the calling hook consumes stdin for its JSON payload, so
an inline `python - <<'PY'` heredoc receives nothing and runs an empty program —
silently.

FAILS CLOSED. On any error this prints `unknown|<reason>`, and the caller
treats `unknown` as a block with an explicit override. A check that cannot read
its subject has no evidence that nothing is running; it must not be mistaken
for evidence that nothing is.
"""
import os
import sys

try:
    from dotenv import load_dotenv

    # A bare `load_dotenv()` searches the CWD, which is the worktree the hook
    # was invoked from — and a git worktree has no `.env`. It therefore found
    # nothing, the connection details were absent, and the probe reported
    # `unknown|` from every worktree: the trees where the migration work
    # actually happens. Load the primary checkout's `.env` explicitly, then let
    # a CWD-local one override it if there is one.
    sun = os.environ.get("CLAUDE_PROJECT_DIR") or "/home/lazycat/github/projects/sun"
    load_dotenv(os.path.join(sun, "trading-service", ".env"))
    load_dotenv(override=False)
    import pymongo

    uri = os.environ.get("PRISM_MONGO_URI") or os.environ.get("MONGO_URI")
    if not uri:
        print("unknown|no PRISM_MONGO_URI in the environment")
        sys.exit(0)

    db_name = os.environ.get("TRADING_MONGO_DB") or "trading_bot"
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=8000)
    doc = client[db_name]["pipeline_state"].find_one({"singleton_id": "current"})
    client.close()

    if not doc:
        # No row is a legitimate idle state: a store that has never run a cycle
        # has nothing to strand.
        print("idle|")
    else:
        print(f"{doc.get('status') or 'unknown'}|{doc.get('cycle_id') or ''}")
except Exception as exc:  # noqa: BLE001 - the reason is the useful part
    print(f"unknown|{type(exc).__name__}: {exc}"[:300])
