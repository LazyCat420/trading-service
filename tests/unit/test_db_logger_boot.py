"""The DB error logger must be registered by the BOOT PATH, not by accident.

Regression for the 2026-07-31 → 08-02 outage: setup_db_logger() only ran when
app.services.logging happened to be imported, which after the tool-telemetry
move to app/v3/tool_telemetry.py was never — so execution_errors and
cycle_audit_log went silent while the container logged hundreds of WARN/ERR
lines. cycle_main.setup_error_capture() is the eager fix; these tests pin its
two load-bearing properties.
"""

import logging

import pytest

from app.services.logging.unified_logger import DbLoggingHandler


def _strip_db_handlers():
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, DbLoggingHandler):
            root.removeHandler(h)


@pytest.fixture(autouse=True)
def clean_root_logger():
    _strip_db_handlers()
    yield
    _strip_db_handlers()


def test_setup_error_capture_registers_handler(monkeypatch):
    """The boot path itself must attach the handler — no HTTP traffic needed."""
    import cycle_main

    # The canary WARNING would try to write to the DB; keep the test hermetic.
    monkeypatch.setattr(DbLoggingHandler, "_write_to_db", lambda self, *a, **k: None)

    logging.basicConfig(level=logging.INFO, force=True)
    cycle_main.setup_error_capture()

    assert any(
        isinstance(h, DbLoggingHandler) for h in logging.getLogger().handlers
    )


def test_basicconfig_force_true_strips_handlers():
    """The trap that made call ORDER load-bearing: force=True removes every
    existing root handler, so registration before basicConfig is a no-op."""
    logging.getLogger().addHandler(DbLoggingHandler())
    logging.basicConfig(level=logging.INFO, force=True)

    assert not any(
        isinstance(h, DbLoggingHandler) for h in logging.getLogger().handlers
    )


def test_setup_error_capture_raises_if_registration_undone(monkeypatch):
    """If something eats the handler, boot must fail loudly, not run blind."""
    import cycle_main

    monkeypatch.setattr(
        "app.services.logging.unified_logger.setup_db_logger", lambda: None
    )
    monkeypatch.setattr("app.services.logging.setup_db_logger", lambda: None)

    logging.basicConfig(level=logging.INFO, force=True)
    with pytest.raises(RuntimeError, match="DB error logger"):
        cycle_main.setup_error_capture()
