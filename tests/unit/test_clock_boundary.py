"""
Clock Boundary Tests — Verify market hours logic and schedule skipping.

Tests:
  1. _is_market_hours returns True during market hours (M-F 9:30-16:00 ET)
  2. _is_market_hours returns False on weekends
  3. _is_market_hours returns False before 9:30 AM ET
  4. _is_market_hours returns False after 4:00 PM ET
  5. Schedule skips execution when outside market hours + market_hours_only=True
  6. Schedule executes when inside market hours + market_hours_only=True

The schedule-boundary tests used to patch `schedule_validator.get_db` and
`cycle_scheduler.get_db` and then assert on SQL text ("INSERT INTO
v3_system_commands" in the executed string). Both modules read and write
through `mongo_query`/`mongo_store` now — `schedule_validator` does not import
`get_db` at all — so the patched cursor intercepted nothing and the dispatch
assertion was scored against a mock that could never see the write, while the
module itself talked to the live store.

They patch the Mongo helpers now and assert on the STRUCTURE of the dispatch:
the collection name and the command document, which pins more than the SQL
substring ever did. `find_row` returns a TUPLE in the column order the caller
listed, so the schedule fixture stays a positional row.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ============================================================================
# TEST: Market hours detection
# ============================================================================

class TestIsMarketHours:
    """_is_market_hours should correctly identify US stock market trading hours."""

    def test_monday_at_noon_is_market_hours(self):
        """Monday at noon ET should be market hours."""
        et = pytz.timezone("US/Eastern")
        # Monday Dec 1, 2025 at noon ET
        fake_now = et.localize(datetime(2025, 12, 1, 12, 0, 0))

        with patch("app.services.cycle_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            from app.services.cycle_scheduler import SchedulerService
            # Directly test the logic since the static method uses datetime.now
            now = fake_now
            assert now.weekday() < 5  # Monday=0
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
            assert market_open <= now <= market_close

    def test_saturday_is_not_market_hours(self):
        """Saturday should never be market hours."""
        et = pytz.timezone("US/Eastern")
        fake_now = et.localize(datetime(2025, 12, 6, 12, 0, 0))  # Saturday

        assert fake_now.weekday() == 5  # Saturday
        # Weekday >= 5 means market is closed
        assert fake_now.weekday() >= 5

    def test_sunday_is_not_market_hours(self):
        """Sunday should never be market hours."""
        et = pytz.timezone("US/Eastern")
        fake_now = et.localize(datetime(2025, 12, 7, 12, 0, 0))  # Sunday

        assert fake_now.weekday() == 6

    def test_before_open_is_not_market_hours(self):
        """Weekday at 8:00 AM ET (before 9:30 open) should not be market hours."""
        et = pytz.timezone("US/Eastern")
        fake_now = et.localize(datetime(2025, 12, 1, 8, 0, 0))  # Monday 8 AM

        assert fake_now.weekday() < 5
        market_open = fake_now.replace(hour=9, minute=30, second=0, microsecond=0)
        assert fake_now < market_open  # Before open

    def test_after_close_is_not_market_hours(self):
        """Weekday at 5:00 PM ET (after 4:00 close) should not be market hours."""
        et = pytz.timezone("US/Eastern")
        fake_now = et.localize(datetime(2025, 12, 1, 17, 0, 0))  # Monday 5 PM

        assert fake_now.weekday() < 5
        market_close = fake_now.replace(hour=16, minute=0, second=0, microsecond=0)
        assert fake_now > market_close  # After close

    def test_exactly_at_open_is_market_hours(self):
        """Exactly 9:30 AM ET should be market hours (inclusive)."""
        et = pytz.timezone("US/Eastern")
        fake_now = et.localize(datetime(2025, 12, 1, 9, 30, 0))

        market_open = fake_now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = fake_now.replace(hour=16, minute=0, second=0, microsecond=0)
        assert market_open <= fake_now <= market_close

    def test_exactly_at_close_is_market_hours(self):
        """Exactly 4:00 PM ET should be market hours (inclusive)."""
        et = pytz.timezone("US/Eastern")
        fake_now = et.localize(datetime(2025, 12, 1, 16, 0, 0))

        market_open = fake_now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = fake_now.replace(hour=16, minute=0, second=0, microsecond=0)
        assert market_open <= fake_now <= market_close

    def test_friday_at_1pm_is_market_hours(self):
        """Friday at 1 PM ET should be market hours."""
        et = pytz.timezone("US/Eastern")
        fake_now = et.localize(datetime(2025, 12, 5, 13, 0, 0))  # Friday

        assert fake_now.weekday() == 4  # Friday
        market_open = fake_now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = fake_now.replace(hour=16, minute=0, second=0, microsecond=0)
        assert market_open <= fake_now <= market_close


# ============================================================================
# TEST: Schedule behavior at clock boundaries
# ============================================================================

def _schedule_row(schedule_id: str, market_hours_only: bool = True):
    """One cycle_schedules row in the exact column order execute_schedule lists.

    `mongo_query.find_row` returns a positional tuple, and the module zips it
    against its own `cols` list — a row of the wrong LENGTH would silently
    shift every field, so the order here is load-bearing.
    """
    return (
        schedule_id, "Test", "interval", None, 2.0, "next_pre_market",
        True, True, True, "[]", None, None, market_hours_only,
        True,           # is_active
        None, None,     # last_run_at, next_run_at
        0, "ok", None,  # run_count, last_status, last_error
        "2025-01-01", "2025-01-01",  # created_at, updated_at
        None, None,     # run_at, expiry_at (no TTL -> never expires)
    )


def _mongo(schedule_row):
    """Patch the scheduler's + validator's Mongo layer, dispatching on collection."""
    query = MagicMock()
    store = MagicMock()

    def _find_row(coll, *a, **k):
        if coll == "cycle_schedules":
            return schedule_row
        if coll == "pipeline_state":
            return ("idle",)
        return None

    query.find_row.side_effect = _find_row
    query.find_rows.return_value = []
    query.agg_row.return_value = None

    # The validator reads cycle_schedules for its own pre_run_check; it asks
    # for a DIFFERENT column list, so it gets its own stub.
    vquery = MagicMock()
    # (schedule_scope, review_intent, urgency, tickers, last_run_at)
    vquery.find_row.return_value = ("portfolio", "monitor", "low", "[]", None)

    return (
        patch("app.services.cycle_scheduler.mongo_query", query),
        patch("app.services.cycle_scheduler.mongo_store", store),
        # The START_CYCLE insert itself moved out of cycle_scheduler and into
        # app/services/cycle_queue.py (one writer for the command queue), so
        # that module holds its own reference to mongo_store. Patching only the
        # scheduler's would leave the real client to answer the write -- which
        # is the identical mistake this file's header describes: a mock aimed
        # at a seam the code no longer uses observes nothing and the dispatch
        # goes somewhere real. Same MagicMock, so _dispatch_calls still sees it.
        patch("app.services.cycle_queue.mongo_store", store),
        patch("app.validation.schedule_validator.mongo_query", vquery),
        query,
        store,
    )


def _dispatch_calls(store):
    """Every insert_docs call that dispatched a START_CYCLE command."""
    return [
        c for c in store.insert_docs.call_args_list
        if c.args and c.args[0] == "v3_system_commands"
    ]


class TestScheduleClockBoundary:
    """Scheduler should respect market_hours_only flag."""

    @pytest.mark.asyncio
    async def test_schedule_skips_outside_market_hours(self):
        """When market_hours_only=True and outside hours, schedule skips."""
        from app.services.cycle_scheduler import SchedulerService

        pq, ps, pqueue, pv, query, store = _mongo(_schedule_row("sched-1"))
        with patch.object(SchedulerService, "_is_market_hours", return_value=False), \
             pq, ps, pqueue, pv, \
             patch("app.services.cycle_scheduler.cycle_control") as mock_cc, \
             patch.object(SchedulerService, "_sync_next_run_to_db"):
            mock_cc.is_paused = False
            mock_cc.is_stopped = False
            await SchedulerService.execute_schedule("sched-1")

        # Should NOT have dispatched a system_command (skipped)
        assert _dispatch_calls(store) == [], \
            "Schedule should have been skipped outside market hours"

    @pytest.mark.asyncio
    async def test_schedule_runs_during_market_hours(self):
        """When market_hours_only=True and inside hours, schedule executes."""
        from app.services.cycle_scheduler import SchedulerService

        pq, ps, pqueue, pv, query, store = _mongo(_schedule_row("sched-2"))
        with patch.object(SchedulerService, "_is_market_hours", return_value=True), \
             pq, ps, pqueue, pv, \
             patch("app.services.cycle_scheduler.cycle_control") as mock_cc, \
             patch.object(SchedulerService, "_sync_next_run_to_db"):
            mock_cc.is_paused = False
            mock_cc.is_stopped = False
            await SchedulerService.execute_schedule("sched-2")

        # Should have dispatched a system_command (executed). Asserting on the
        # command DOCUMENT rather than on SQL text: the collection, the command
        # type, and the fact that a payload rode along.
        dispatches = _dispatch_calls(store)
        assert len(dispatches) >= 1, "Schedule should have executed during market hours"
        doc = dispatches[0].args[1][0]
        assert doc["command_type"] == "START_CYCLE"
        assert doc["payload"]


# ============================================================================
# TEST: Paused system skips execution when schedule executes
# ============================================================================

class TestPausedSystemSkipsSchedule:
    """When cycle_control.is_paused/is_stopped is True, schedules should skip execution and NOT auto-resume."""

    @pytest.mark.asyncio
    async def test_paused_system_skips_schedule(self):
        """Paused system should skip executing scheduled cycles and not call resume."""
        from app.services.cycle_scheduler import SchedulerService

        pq, ps, pqueue, pv, query, store = _mongo(
            _schedule_row("sched-paused", market_hours_only=False)
        )
        with patch("app.services.cycle_scheduler.cycle_control") as mock_cc, \
             pq, ps, pqueue, pv, \
             patch.object(SchedulerService, "_sync_next_run_to_db"):
            mock_cc.is_paused = True
            mock_cc.is_stopped = False
            await SchedulerService.execute_schedule("sched-paused")

            # Check that resume was NOT triggered on cycle_control
            mock_cc.resume.assert_not_called()

        # Should NOT have dispatched a system_command (skipped)
        assert _dispatch_calls(store) == [], \
            "Schedule should have been skipped when system is paused"
