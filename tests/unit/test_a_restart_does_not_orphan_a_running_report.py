"""A crash mid-audit left a report `running` forever, and the panel spun on it.

`BootService._reset_app_state` marks a crashed container's `pipeline_state`,
`v3_system_commands` and `system_commands` as error on the next boot. It never
touched `autoresearch_reports`, so a report whose worker died mid-audit stayed
`status: running` with no process behind it. Two things then kept it that way:
the janitor's `_clean_stale_reports` (30-minute cutoff -> `stale`) only runs
inside a cycle, and the service boots PAUSED, so no cycle runs to trigger it;
and the client's `/autoresearch/status` computes `is_running` as
`bool(row or report_status == 'running')`, so the orphan keeps the spinner on
indefinitely. Found by the report-chain audit on 2026-09-06.

`interrupted` is already in the report vocabulary (rendered amber, "scores
were never computed"); it is the honest state for a run the container took
down, and it is not `error`, which would say the audit itself failed.
"""
from __future__ import annotations

from unittest.mock import patch

from app.services.boot_service import BootService


def _reset_calls():
    with patch("app.services.boot_service.mongo_store") as ms:
        BootService._reset_app_state()
    return [(c.args[0], c.args[1], c.args[2]) for c in ms.update_docs.call_args_list
            if c.args and c.args[0] == "autoresearch_reports"]


def test_a_running_report_is_marked_interrupted_on_boot():
    calls = _reset_calls()
    assert calls, "boot never touches autoresearch_reports, so a crashed audit stays 'running' forever"
    coll, query, update = calls[0]
    assert query.get("status") == "running" or "running" in (query.get("status") or {}).get("$in", [])
    assert update["$set"]["status"] == "interrupted", (
        "a run the container took down was interrupted, not an audit that failed"
    )


def test_it_does_not_touch_finished_reports():
    for coll, query, update in _reset_calls():
        status = query.get("status")
        matched = status.get("$in") if isinstance(status, dict) else [status]
        assert "done" not in matched and "error" not in matched
