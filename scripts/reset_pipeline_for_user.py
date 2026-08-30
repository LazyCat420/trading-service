"""Clear a stuck pipeline: idle the state row, fail any running command.

Converted off Postgres 2026-08-30. It used to UPDATE the frozen archive, so it
reported success while the live Mongo control plane stayed exactly as stuck as
before — the same shape as the cutover's other stranded guards.
"""
from app.db import mongo_store

state = mongo_store.update_docs(
    "pipeline_state",
    {"status": {"$in": ["starting", "running"]}},
    {"$set": {"status": "idle", "cycle_id": None, "progress": ""}},
)
cmds = mongo_store.update_docs(
    "v3_system_commands",
    {"status": "running"},
    {"$set": {"status": "error"}},
)
print(f"Pipeline reset ({state} state row(s)); {cmds} running command(s) marked error.")
