"""Failures inside the DB log handler are counted, not silently swallowed.

The handler's blanket except/pass made a dead capture path indistinguishable
from a quiet system (the 07-31→08-02 outage). Drops now increment a shared
counter and surface on stderr — stderr, never logging, which would recurse
into the handler itself.
"""

import logging

import pytest

from app.services.logging import unified_logger
from app.services.logging.unified_logger import DbLoggingHandler


@pytest.fixture(autouse=True)
def reset_counter():
    DbLoggingHandler.dropped = 0
    yield
    DbLoggingHandler.dropped = 0


def _warning_record(msg: str = "boom") -> logging.LogRecord:
    return logging.LogRecord(
        name="app.services.watch_desk", level=logging.WARNING,
        pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None,
    )


@pytest.fixture
def failing_db(monkeypatch):
    """Make the handler's write fail.

    The fault used to be injected at `scripts.migration.pg_connection.get_db`. The handler
    writes both rows through `mongo_store.insert_docs` now, so that is the
    surface that has to explode for the drop counter to see anything.

    `dropped` is a CLASS attribute, and a `DbLoggingHandler` is registered on
    the root logger for the whole session. Anything logged while this fixture
    is active — including `emit()`'s own lazy imports — reaches that handler
    too and charges extra drops to the same counter. So the fixture also
    detaches every DbLoggingHandler from the root logger for its duration,
    leaving the handler the test constructs as the only one counting.

    (The old fixture patched `scripts.migration.pg_connection.get_db`, which the handler had
    already stopped using, so the counter never moved at all and neither the
    fault nor this interference was visible.)
    """
    import app.services.logging.unified_logger as ul

    root = logging.getLogger()
    detached = [h for h in root.handlers if isinstance(h, DbLoggingHandler)]
    for h in detached:
        root.removeHandler(h)

    class _Exploding:
        def __getattr__(self, name):
            def explode(*a, **k):
                raise RuntimeError("db unreachable")
            return explode

    monkeypatch.setattr(ul, "mongo_store", _Exploding())
    try:
        yield
    finally:
        for h in detached:
            root.addHandler(h)


def test_a_db_failure_increments_the_counter_and_hits_stderr(failing_db, capsys):
    handler = DbLoggingHandler()
    handler.emit(_warning_record())

    assert DbLoggingHandler.dropped == 1
    err = capsys.readouterr().err
    assert "dropped 1 record(s)" in err
    assert "db unreachable" in err


def test_drops_are_reported_first_then_every_nth(failing_db, capsys):
    handler = DbLoggingHandler()
    for _ in range(unified_logger._DROP_LOG_EVERY + 1):
        handler.emit(_warning_record())

    assert DbLoggingHandler.dropped == unified_logger._DROP_LOG_EVERY + 1
    err = capsys.readouterr().err
    # One line for the first drop, one at the _DROP_LOG_EVERY mark — not 51.
    assert err.count("[UnifiedLogger] DB error capture has dropped") == 2


def test_a_healthy_write_counts_nothing(monkeypatch, capsys):
    monkeypatch.setattr(DbLoggingHandler, "_write_to_db", lambda self, *a, **k: None)
    handler = DbLoggingHandler()
    handler.emit(_warning_record())

    assert DbLoggingHandler.dropped == 0
    assert capsys.readouterr().err == ""
