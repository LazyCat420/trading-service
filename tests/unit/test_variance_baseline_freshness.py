"""The noise floor must report its own age, and something must refresh it.

`run_and_persist` had no scheduled caller — only a manual POST and
scripts/decision_variance.py. Measured 2026-09-04, variance_runs held exactly
one row: a 4-run measurement on NVDA from 2026-07-20, 46 days old. Every
"clears the noise floor" claim on the eval-trust panel rested on it, and
nothing on the page said how old it was.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app.routers.eval_trust_router as etr
from app.services.cycle_scheduler import SchedulerService


def _variance_payload(rows):
    with patch.object(etr, "mongo_query") as mq, \
         patch.object(etr, "mongo_store") as ms:
        mq.find_rows.return_value = rows
        ms.find_docs.return_value = []
        import asyncio
        return asyncio.run(etr.variance_runs())


def _row(days_old):
    ts = datetime.now(timezone.utc) - timedelta(days=days_old)
    return ("vr-1", "cyc", "NVDA", 6, 6, '{"BUY": 6}', "BUY", 0.0,
            72.0, 1.26, "[71, 74]", "done", None, ts, ts)


def test_a_stale_baseline_is_reported_as_stale():
    out = _variance_payload([_row(46)])
    assert out["newest_run_age_days"] == 46.0 or 45.9 <= out["newest_run_age_days"] <= 46.1
    assert out["stale"] is True


def test_a_fresh_baseline_is_not_stale():
    out = _variance_payload([_row(2)])
    assert out["stale"] is False


def test_no_runs_at_all_is_stale_not_silent():
    """Zero measurements must not read the same as a recent one."""
    out = _variance_payload([])
    assert out["newest_run_age_days"] is None
    assert out["stale"] is True


def test_the_documented_baseline_is_still_labelled_as_such():
    """It stays available as a fallback, but never masquerades as a run."""
    out = _variance_payload([])
    assert out["baseline"]["source"].startswith("handoff-eval-trust-wave")
    assert out["baseline"]["measured_at"] == "2026-07-19"
    assert out["runs"] == []


def test_something_actually_refreshes_it():
    """The gap was not the number, it was that nothing recomputed it."""
    assert hasattr(SchedulerService, "_run_variance_baseline")
    out = _variance_payload([_row(1)])
    assert "weekly" in out["refresh_schedule"]
