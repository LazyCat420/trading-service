"""The autoresearch queue's payload is a DOCUMENT, and a claimed job must be released.

`PipelineService` enqueues AUTORESEARCH with a dict payload deliberately
("one enqueue, one shape", pipeline_service.py:2355). The Postgres column was
JSON text, and the codemod left `json.loads(payload_str)` in the consumer.
After the cutover that raised on the very first post-cycle job —
cycle-v3-1787193855's `job_a000e299`:

    [eval_worker] INFO  Found pending AUTORESEARCH command: job_a000e299
    [eval_worker] ERROR Error polling system_commands: the JSON object must be
                        str, bytes or bytearray, not dict

The job had already been marked `running`, and the raise happened ABOVE the
inner try, so nothing marked it failed: it stayed claimed forever, the poller
looped every 5s finding nothing pending, and the cycle got no autoresearch
report — no reflection, no directives, no learning signal. Silently, one stuck
row per cycle.

Two things are pinned here, because the second is what turned a type error into
a permanently stalled queue:

  * a dict payload is passed through, a str payload is still parsed (older rows
    and every other producer write JSON text);
  * a payload that genuinely cannot be read marks the command `error` — a
    claimed job is always released, one way or the other.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.autoresearch import eval_worker


class _Stop(Exception):
    """Breaks the poller's `while True` after one iteration."""


def _run_one_poll(cmd_row, runner):
    """Drive exactly one iteration of the poller's infinite loop."""
    updates = []
    store = MagicMock()
    store.update_docs.side_effect = lambda *a, **k: updates.append(a)
    query = MagicMock()
    query.find_row.return_value = cmd_row

    async def _sleep(_seconds):
        raise _Stop()

    with patch.object(eval_worker, "mongo_store", store), \
         patch.object(eval_worker, "mongo_query", query), \
         patch.object(eval_worker, "run_autoresearch", runner), \
         patch.object(eval_worker.asyncio, "sleep", _sleep):
        with pytest.raises(_Stop):
            asyncio.run(eval_worker.poll_system_commands())
    return updates


def test_a_dict_payload_reaches_the_runner():
    """The regression: this is the shape every AUTORESEARCH job has."""
    runner = AsyncMock()
    _run_one_poll(("job_1", "AUTORESEARCH", {"cycle_id": "cycle-v3-1", "cycle_summary": {}}), runner)
    runner.assert_awaited_once()
    job_id, payload = runner.await_args[0]
    assert job_id == "job_1"
    assert payload == {"cycle_id": "cycle-v3-1", "cycle_summary": {}}


def test_a_json_string_payload_is_still_parsed():
    """Older rows and the other producers write JSON text; both must work."""
    runner = AsyncMock()
    _run_one_poll(("job_2", "AUTORESEARCH", json.dumps({"cycle_id": "cycle-v3-2"})), runner)
    assert runner.await_args[0][1] == {"cycle_id": "cycle-v3-2"}


def test_an_empty_payload_is_an_empty_dict():
    runner = AsyncMock()
    _run_one_poll(("job_3", "AUTORESEARCH", None), runner)
    assert runner.await_args[0][1] == {}


def test_an_unreadable_payload_releases_the_job_as_error():
    """A claimed job must never stay claimed.

    Before the fix the parse sat above the inner try, so any failure escaped to
    the outer handler with the row already at `running` — the queue's one
    permanent-stall path.
    """
    runner = AsyncMock()
    updates = _run_one_poll(("job_4", "AUTORESEARCH", "{not json"), runner)
    runner.assert_not_awaited()
    statuses = [u[2]["$set"].get("status") for u in updates if len(u) > 2]
    assert "running" in statuses
    assert "error" in statuses, (
        "the job was claimed and never released — this is the stall that cost "
        "cycle-v3-1787193855 its autoresearch report")
