"""A partial state write must not tell the deploy interlocks the desk is quiet.

`PipelineStateDB.get_state` was hardened after a Mongo read fault answered the
guards with `default_state()` — status "idle", a member of IDLE_STATUSES in
both interlocks — and a deploy killed a live cycle, losing EXLS/OWL/CARS/GM on
2026-07-27. Its comment says so at length.

The WRITE side kept the same shape: `state.get("status", "idle")`. Any caller
handing save_state a dict without a status published "no cycle is running".

It happened again on 2026-08-31: `scripts/deploy_preflight.py` — the
open-item-45 gate that exists to stop exactly this, and which fails CLOSED on
an unreadable state — printed

    [deploy_preflight] pipeline idle (status=idle) — deploy may proceed

at 23:29:14Z while cycle-observe-1788217529 was still making synthesizer LLM
turns. The container swap 49 seconds later cancelled it: status=stopped,
"Cycle stopped/cancelled", analysis_results_count=0, no final decision on the
desk. DEPLOY_SKIP_CYCLE_CHECK was not used; the gate ran and was told idle.

Whether this default produced that particular read is UNKNOWN — pipeline_state
keeps no history, so the moment cannot be replayed. What is CONFIRMED is that
it is a path to the same wrong answer, and these tests close it. "unknown" is
in neither guard's IDLE_STATUSES, so an incomplete write now blocks a deploy.
"""

from unittest.mock import patch

import pytest

from app.services.pipeline_state import PipelineStateDB

#: Both interlocks: .claude/hooks/guard_deploy.py and
#: trading-service/scripts/deploy_preflight.py. Kept literal here on purpose —
#: importing them would make this test agree with whatever they say today.
IDLE_STATUSES = {"idle", "done", "error", "stopped", "interrupted"}


def _captured_status(state: dict) -> str:
    seen = {}

    def _fake_upsert(collection, key, doc, *a, **kw):
        seen["collection"] = collection
        seen["doc"] = doc

    with patch("app.services.pipeline_state.mongo_store.upsert_doc", _fake_upsert):
        PipelineStateDB.save_state(state)

    assert seen["collection"] == "pipeline_state"
    return seen["doc"]["status"]


class TestAMissingStatusIsNeverIdle:
    def test_an_empty_state_writes_unknown(self):
        """The assertion that fails on the old `state.get("status", "idle")`."""
        assert _captured_status({}) == "unknown"

    def test_a_partial_state_writes_unknown(self):
        """`load_state(summary_only=True)` round-trips shapes like this."""
        partial = {"cycle_id": "cycle-v3-1788208223", "progress": "Analyzing..."}

        assert _captured_status(partial) == "unknown"

    def test_the_written_status_is_not_one_the_guards_call_quiet(self):
        """The property that actually matters, stated as itself."""
        assert _captured_status({}) not in IDLE_STATUSES

    def test_a_blank_status_is_treated_as_missing(self):
        assert _captured_status({"status": ""}) == "unknown"
        assert _captured_status({"status": None}) == "unknown"


class TestARealStatusIsPassedThroughUntouched:
    """The other direction: this must not become 'always unknown'."""

    @pytest.mark.parametrize("status", ["running", "starting", "idle", "done", "stopped"])
    def test_it_is_written_verbatim(self, status):
        assert _captured_status({"status": status, "cycle_id": "c"}) == status

    def test_a_deliberate_idle_still_works(self):
        """default_state() is a legitimate caller — resetting after a cycle."""
        assert _captured_status(PipelineStateDB.default_state()) == "idle"


class TestSwitchingProfilesCannotClobberALiveCycle:
    """bot_manager.set_active_bot resets the singleton to idle as its last act.

    That protection already exists — `is_cycle_running()` raises before the
    reset — and this pins it, because the reset itself is unguarded and would
    publish "idle" over a running cycle if the check above it were ever
    relaxed. Worth noting for whoever revisits it: `is_cycle_running` reads
    the SAME pipeline_state row, so it inherits whatever that row says. The
    real defence is the write side above, not this check.
    """

    def _switch(self, status: str):
        from app.services import bot_manager

        reset = {"called": False}

        with patch("app.services.pipeline_service.PipelineService.get_current_state",
                   lambda summary_only=False: {"status": status}), \
             patch("app.services.pipeline_state.PipelineStateDB.save_state",
                   lambda state: reset.__setitem__("called", True)), \
             patch.object(bot_manager.mongo_query, "find_row", lambda *a, **kw: ("bot-x",)), \
             patch.object(bot_manager.mongo_store, "update_docs", lambda *a, **kw: None):
            bot_manager.set_active_bot("bot-x")
        return reset["called"]

    @pytest.mark.parametrize("status", ["running", "analyzing", "starting", "collecting"])
    def test_it_refuses_while_a_cycle_runs(self, status):
        from app.services import bot_manager

        with pytest.raises(ValueError, match="while a pipeline cycle is running"):
            self._switch(status)

    def test_it_still_resets_when_the_desk_is_quiet(self):
        """A guard that never lets the reset happen would be a different bug."""
        assert self._switch("idle")
        assert self._switch("done")
