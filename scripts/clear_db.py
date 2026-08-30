"""Clear a stuck pipeline: idle the state row, complete pending commands.

Converted off Postgres 2026-08-30 — it used to update the frozen archive and
print "DB cleared" while the live control plane was untouched.
"""
from app.db import mongo_store

state = mongo_store.update_docs(
    "pipeline_state",
    {"singleton_id": "current"},
    {"$set": {"status": "idle", "cycle_id": None, "progress": ""}},
)
legacy = mongo_store.update_docs(
    "system_commands", {"status": "pending"}, {"$set": {"status": "completed"}},
)
v3 = mongo_store.update_docs(
    "v3_system_commands", {"status": "pending"}, {"$set": {"status": "completed"}},
)
print(f"DB cleared: {state} state row(s), {v3} v3 command(s), {legacy} legacy command(s)")
