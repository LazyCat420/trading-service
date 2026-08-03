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
    def explode(*a, **k):
        raise RuntimeError("db unreachable")
    monkeypatch.setattr("app.db.connection.get_db", explode)


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
