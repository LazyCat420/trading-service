"""The last-analysis lookup must not depend on MONGO_STORE_BACKEND.

app/db/mongo_store.py keeps a per-table backend map whose default is "pg".
That map never reached the store's own read/write helpers -- aggregate(),
find_docs() and friends always hit Mongo -- so a reads_mongo() gate in front
of a Mongo aggregate could only ever subtract behaviour.

In the funnel it did exactly that. With MONGO_STORE_BACKEND unset (any local
run, any container that missed deploy.sh's env resolution) backend_for()
returned its "pg" default, the aggregate was skipped, and last_analysis_map
came out empty: every candidate ticker scored as never-analysed, so the
recency penalty and the freshness baseline silently vanished with no error
in any log.

The check reads the source instead of driving a cycle: the failure is
structural, the read is either gated or it is not. Reading by path also keeps
this guard runnable without the lazycat SDK on PYTHONPATH.
"""
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "app" / "services" / "pipeline_service.py"


def _last_analysis_region() -> str:
    src = _SRC.read_text()
    anchor = src.index('aggregate("analysis_results"')
    return src[max(0, anchor - 3000):anchor + 1500]


def test_source_file_is_where_we_think_it_is():
    """Vacuity guard: an unreadable file must fail loudly, not pass silently."""
    assert _SRC.is_file(), f"{_SRC} not found; re-point this guard"
    assert 'aggregate("analysis_results"' in _SRC.read_text(), (
        "the last-analysis aggregate moved or was renamed; re-point this guard"
    )


def test_last_analysis_aggregate_is_not_behind_reads_mongo():
    region = _last_analysis_region()
    gated = [
        line.strip() for line in region.splitlines()
        if "reads_mongo(" in line and "analysis_results" in line
    ]
    assert not gated, (
        "the last-analysis read is gated on reads_mongo('analysis_results') "
        "again. With MONGO_STORE_BACKEND unset that gate is False and every "
        "ticker silently reads as never-analysed. Found: " + repr(gated)
    )


def test_the_trap_this_guards_against_is_still_real():
    """If the backend default stops being "pg", retire this guard with the
    flag machinery rather than weakening it."""
    import os
    import importlib
    from app.db import mongo_store

    saved = os.environ.pop("MONGO_STORE_BACKEND", None)
    try:
        reloaded = importlib.reload(mongo_store)
        assert reloaded.backend_for("analysis_results") == "pg"
        assert reloaded.reads_mongo("analysis_results") is False
    finally:
        if saved is not None:
            os.environ["MONGO_STORE_BACKEND"] = saved
        importlib.reload(mongo_store)
