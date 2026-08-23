"""A failed prior-trade-history read must say so, not silently return ''.

31% of desks carry no prior-trade lines (audit 2026-08-23); without the log a
DB failure is indistinguishable from a ticker with no history.
"""

import logging
from unittest.mock import patch

from app.agents.base_agent import get_ticker_outcome_context


def test_failed_read_returns_empty_and_warns(caplog):
    with patch("app.db.mongo_query.find_rows", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.WARNING, logger="app.agents.base_agent"):
            got = get_ticker_outcome_context("AAPL")
    assert got == ""
    assert any("prior-trade history read failed for AAPL" in r.message
               and "RuntimeError" in r.message for r in caplog.records), \
        "the swallowed exception must be named in a warning"


def test_synthetic_ticker_short_circuits_without_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.agents.base_agent"):
        assert get_ticker_outcome_context("_AUDIT_") == ""
    assert not caplog.records
