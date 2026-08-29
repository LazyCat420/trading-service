"""The one writer of START_CYCLE commands.

Five producers can start a cycle -- the schedule cadence, the market-open job,
a Watch Desk wake, the research governor, and the UI -- and each used to carry
its own copy of the same insert. The copies agreed, which is exactly why they
were dangerous: the queue NAME is the contract with the worker
(`cycle_main.poll_system_commands` drains `v3_system_commands`), and a
copy-pasted writer is how one producer ends up addressing a queue nothing
drains. That already happened on the client side, which writes its commands to
`system_commands` and has been dispatching into a void.

Anything that starts a cycle goes through enqueue_start_cycle().
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from app.db import mongo_store

logger = logging.getLogger(__name__)

#: The queue `cycle_main.poll_system_commands` actually drains. Not
#: `system_commands`, which is the autoresearch eval worker's separate queue.
COMMAND_COLLECTION = "v3_system_commands"


def enqueue_start_cycle(payload: dict, *, prefix: str) -> str:
    """Queue a START_CYCLE command and return its id.

    `prefix` identifies the producer in the command id (sch-cmd, sch-open, wd,
    gov-...), which is what makes a queued cycle traceable back to whatever
    decided to start it.
    """
    cmd_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
    mongo_store.insert_docs(COMMAND_COLLECTION, [{
        "id": cmd_id,
        "command_type": "START_CYCLE",
        "payload": json.dumps(payload),
        "status": "pending",
        "progress": 0,
        "created_at": datetime.now(timezone.utc),
    }])
    return cmd_id


def enqueue_refresh_schedule(job_id: str, *, prefix: str = "sch-refresh") -> str:
    """Queue a REFRESH_SCHEDULE command and return its id."""
    cmd_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
    mongo_store.insert_docs(COMMAND_COLLECTION, [{
        "id": cmd_id,
        "command_type": "REFRESH_SCHEDULE",
        "payload": json.dumps({"job_id": job_id}),
        "status": "pending",
        "progress": 0,
        "created_at": datetime.now(timezone.utc),
    }])
    return cmd_id
