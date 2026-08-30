"""Fail every stuck command and idle the pipeline state row.

Converted off Postgres 2026-08-30 — it used to update the frozen archive and
print "Fixed DB stuck states" while nothing live had moved.
"""
from app.db import mongo_store

stuck = {"status": {"$in": ["pending", "running"]}}
v3 = mongo_store.update_docs("v3_system_commands", stuck, {"$set": {"status": "error"}})
legacy = mongo_store.update_docs("system_commands", stuck, {"$set": {"status": "error"}})
state = mongo_store.update_docs(
    "pipeline_state", {"singleton_id": "current"}, {"$set": {"status": "idle"}},
)
print(f"Fixed DB stuck states: {v3} v3 command(s), {legacy} legacy command(s), "
      f"{state} state row(s)")
