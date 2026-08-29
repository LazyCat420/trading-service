"""Guards for the boot-time data tasks.

BootService used to carry its own copies of the FRED / market / SP500 startup
tasks. They drifted: the FRED copy passed SQL WHERE strings ("source = 'fred'")
to `_is_data_fresh`, whose `query` argument is a Mongo filter dict. The call
raised inside the helper's bare `except`, so the helper returned False, the
"already fresh, skipping" branch could never be taken, and every single restart
re-collected FRED regardless of how fresh the data was.

These tests pin the two properties that failure needed: one owner for the
task bodies, and a dict-shaped freshness query.
"""
import asyncio
import inspect
from unittest.mock import AsyncMock, patch

import pytest

from app.services import startup_tasks
from app.services.boot_service import BootService


def test_boot_service_does_not_redefine_startup_tasks():
    """BootService must delegate, not carry a second copy of each task."""
    for name in ("_startup_fred_refresh", "_startup_market_collect",
                 "_startup_sp500_seed"):
        assert not hasattr(BootService, name), (
            f"BootService.{name} is back. These bodies live in "
            "app/services/startup_tasks.py; a second copy is what drifted "
            "into the SQL-string-to-Mongo-helper bug."
        )


def test_fred_freshness_queries_are_mongo_dicts():
    """The freshness probe takes a Mongo filter dict, never a SQL fragment."""
    seen = []

    def fake_is_data_fresh(table, query, max_age_days):
        seen.append(query)
        return True  # pretend everything is fresh

    with patch.object(startup_tasks, "_is_data_fresh", fake_is_data_fresh), \
         patch("app.collectors.fred_collector.sync_collect_fred") as collector, \
         patch("asyncio.sleep", new=AsyncMock()):
        asyncio.run(startup_tasks.startup_fred_refresh(lambda: False))

    assert seen, "the freshness probe was never called"
    for query in seen:
        assert isinstance(query, dict), (
            f"freshness query {query!r} is not a Mongo filter dict — a SQL "
            "WHERE string here is swallowed by _is_data_fresh's except and "
            "silently forces a full re-collect on every boot"
        )
    # Fresh data means the collector must NOT run. On the old boot_service copy
    # this assertion failed: the skip branch was unreachable.
    collector.assert_not_called()


def test_is_data_fresh_returns_false_for_a_sql_string_query():
    """Vacuity guard: prove the old shape really does fail closed."""
    assert startup_tasks._is_data_fresh(
        "macro_indicators", "source = 'fred'", 2
    ) is False


def test_startup_all_is_gone():
    """startup_all had zero callers and duplicated BootService's sequencing."""
    assert not hasattr(startup_tasks, "startup_all"), (
        "startup_all is back; BootService._start_background_tasks owns the "
        "boot sequence."
    )
