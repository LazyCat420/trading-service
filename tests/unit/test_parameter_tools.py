"""Parameter tools — registered, whitelisted to the right agents, and the
write tool round-trips the governor's teach-y rejections."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import app.tools  # noqa: F401 — triggers registration of all tool modules
from app.tools.registry import registry


def _run(coro):
    return asyncio.run(coro)


def test_tools_are_registered_with_implementations():
    for name in ("get_parameters", "propose_parameter_change"):
        func = registry.tools.get(name)
        assert func is not None, f"{name} not registered (or schema-only, func=None)"


def test_whitelists_grant_write_to_pm_and_board_only():
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS as wl

    assert "propose_parameter_change" in wl["v3_portfolio_manager"]
    assert "propose_parameter_change" in wl["v3_board_of_directors"]
    assert "propose_parameter_change" in wl["user_chat"]
    assert "get_parameters" in wl["v3_portfolio_manager"]
    assert "get_parameters" in wl["v3_board_of_directors"]
    # Analysts hold NEITHER grant. They used to hold the read (the original
    # "see the limits but cannot change them" split), but both analysts dropped
    # get_parameters on 2026-07-25: zero calls in 60 days and no prompt line
    # asking for it, because the risk envelope reaches these desks through the
    # precomputed context block rather than a tool call. Asserting the drop, not
    # just the absence of the write, so re-adding a tool no prompt mentions has
    # to be a deliberate edit here too.
    for analyst in ("v3_fundamental_analyst", "v3_quant_analyst"):
        assert "get_parameters" not in wl[analyst]
        assert "propose_parameter_change" not in wl[analyst]
    # Workers with no grant stay without it.
    assert "propose_parameter_change" not in wl.get("v3_junior_analyst", [])


def test_get_parameters_tool_returns_full_registry(monkeypatch):
    from app.services import parameter_store as ps
    from app.tools.parameter_tools import get_parameters

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ps, "get_db", _boom)
    ps.invalidate_cache()

    out = json.loads(_run(get_parameters()))
    assert out["status"] == "ok"
    keys = {p["key"] for p in out["parameters"]}
    assert keys == set(ps.PARAMETER_REGISTRY)


def test_propose_tool_surfaces_governor_rejection(monkeypatch):
    from app.services import parameter_store as ps
    from app.tools.parameter_tools import propose_parameter_change

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ps, "get_db", _boom)
    ps.invalidate_cache()

    out = json.loads(_run(propose_parameter_change(
        key="MAX_POSITION_SIZE_PCT", value=0.99,
        reason="testing an out-of-envelope proposal end to end",
    )))
    assert out["status"] == "rejected"
    assert "envelope" in out["reason"] or "outside" in out["reason"]
