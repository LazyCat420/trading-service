"""Five tools prism force-adds trip the off-whitelist canary on every call.

The tool-surface audit (2026-09-06, 14 days of agent_tool_telemetry) found
read_url, search_web, list_directory, read_mcp_resource and write_datastore
called by v3 agents, succeeding, and absent from `_META_TOOLS` — so every
call logged "[ToolCanary] OFF-WHITELIST", which is the line meant to mark a
real breach. The canary's own message says what to do with a benign,
recurring force-add: put it in `_META_TOOLS` so it stops masking breaches.

Four of the five are read-only and go in. `write_datastore` does not: it
writes, `query_datastore` is on the DENY list, and the canary flagging it is
the canary doing its job — that one is an open policy question, not noise.
"""
from __future__ import annotations

import logging

import pytest

from app.v3 import tool_telemetry
from app.v3.tool_telemetry import _META_TOOLS, _canary_check

READ_ONLY_FORCE_ADDS = ("read_url", "search_web", "list_directory", "read_mcp_resource")


def _offwhitelist_lines(caplog, tool):
    caplog.set_level(logging.WARNING, logger=tool_telemetry.__name__)
    caplog.clear()
    _canary_check("v3_bear_agent", tool, error_message="", was_blocked=False)
    return [r for r in caplog.records if "OFF-WHITELIST" in r.getMessage()]


@pytest.mark.parametrize("tool", READ_ONLY_FORCE_ADDS)
def test_a_read_only_force_add_is_not_a_breach(caplog, tool):
    assert tool in _META_TOOLS
    assert _offwhitelist_lines(caplog, tool) == []


def test_a_write_tool_still_trips_it(caplog):
    assert "write_datastore" not in _META_TOOLS
    assert len(_offwhitelist_lines(caplog, "write_datastore")) == 1
