#!/usr/bin/env python3
"""Print the live pipeline state.

Reads MONGO. This is the command `.claude/hooks/guard_deploy.py` tells an
operator to run when it blocks a deploy ("is a cycle really live?"), so it has
to read the store the pipeline writes. Until 2026-08-19 it read Postgres, where
`pipeline_state` has been a frozen archive since the cutover — it would have
answered "done" through the whole of any live cycle.

    python scripts/check_pipeline_state.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_store  # noqa: E402

FIELDS = ("cycle_id", "status", "phase", "operational_phase", "progress",
          "tickers", "started_at", "finished_at", "updated_at", "error")


def main() -> int:
    doc = mongo_store.get_doc_db()["pipeline_state"].find_one({"singleton_id": "current"})
    if not doc:
        print("pipeline_state: no `current` document — the pipeline has never written state")
        return 0
    print("pipeline_state (mongo, singleton_id='current'):")
    for k in FIELDS:
        if k in doc:
            print(f"  {k:18} {doc.get(k)}")
    extra = sorted(set(doc) - set(FIELDS) - {"_id", "singleton_id"})
    if extra:
        print(f"  {'other fields':18} {', '.join(extra)}")
    print()
    print(json.dumps({k: str(v) for k, v in doc.items() if k != "_id"}, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
