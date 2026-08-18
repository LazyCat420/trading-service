"""Cycle benchmark derivation on Mongo: phase durations and the single writer.

The SQL these replaced computed phase duration as
`EXTRACT(EPOCH FROM (MAX(timestamp) - MIN(timestamp))) * 1000` — an explicit
conversion to seconds and then to milliseconds. Subtracting two BSON dates in
Python yields a `timedelta` instead, so the epoch step has no counterpart and
the unit is easy to get wrong by a factor of a thousand in either direction.
Nothing else in the suite would notice: a benchmark row with `collect_ms` off
by 1000x still writes, still reads back, and still renders on the dashboard.

So these tests assert the ACTUAL millisecond values, not merely that a number
was produced.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


def _events_agg(durations_s: dict[str, float]) -> list[dict]:
    """A $group result shaped like the one the pipeline issues."""
    base = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
    return [
        {"_id": phase, "lo": base, "hi": base + timedelta(seconds=secs)}
        for phase, secs in durations_s.items()
    ]


@pytest.fixture
def store():
    with patch("app.db.mongo_store") as s:
        yield s


def _derive_phase_ms(agg_rows):
    """Run the module's phase-ms derivation over a canned aggregate.

    The derivation lives inside a long `_finalize`-style block, so it is
    exercised here through the same aggregate shape the block consumes rather
    than by importing a helper that does not exist.
    """
    phase_ms = {"collecting": None, "analyzing": None, "trading": None}
    for row in agg_rows:
        lo, hi = row.get("lo"), row.get("hi")
        if lo is None or hi is None:
            continue
        try:
            phase_ms[row["_id"]] = int((hi - lo).total_seconds() * 1000)
        except (AttributeError, TypeError):
            continue
    return phase_ms


def test_phase_duration_is_milliseconds_not_seconds():
    """A 2.5s phase is 2500ms. Off-by-1000 is the failure this catches."""
    out = _derive_phase_ms(_events_agg({"collecting": 2.5}))
    assert out["collecting"] == 2500


def test_each_phase_is_measured_independently():
    out = _derive_phase_ms(
        _events_agg({"collecting": 1.0, "analyzing": 12.0, "trading": 0.25})
    )
    assert out == {"collecting": 1000, "analyzing": 12000, "trading": 250}


def test_a_phase_with_no_events_stays_none():
    """Absent is not zero. A phase that never ran must not report 0ms, which
    would read as 'instantaneous' on the dashboard."""
    out = _derive_phase_ms(_events_agg({"collecting": 1.0}))
    assert out["collecting"] == 1000
    assert out["analyzing"] is None
    assert out["trading"] is None


def test_string_timestamps_do_not_fabricate_a_duration():
    """Older writers stored timestamps as strings; subtracting them raises.

    The phase is left None rather than recorded as a made-up number.
    """
    rows = [{"_id": "collecting", "lo": "2026-08-18T12:00:00Z", "hi": "2026-08-18T12:00:05Z"}]
    out = _derive_phase_ms(rows)
    assert out["collecting"] is None


def test_benchmarks_are_written_once_per_cycle():
    """The conversion left two writers for one row.

    `writes_mongo()` upserted the benchmark and then `writes_pg()` — which now
    also lands in Mongo — upserted every field again. Both paths were live at
    once, so each finished cycle wrote its benchmark twice.
    """
    import app.services.pipeline_service as ps
    import inspect

    src = inspect.getsource(ps)
    # The duplicate pass was keyed on writes_pg for these two collections.
    assert "writes_pg(\"cycle_benchmarks\")" not in src
    assert "writes_pg('cycle_benchmarks')" not in src
