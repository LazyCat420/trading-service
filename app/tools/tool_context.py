"""
Execution context for agent-initiated tool calls.

Whiteboard/peer tools used to read CYCLE_ID / AGENT_NAME from process env
vars that nothing ever set, so every agent-initiated call landed on the
'default_cycle' board as author 'unknown' (2026-07-15 audit). Context now
resolves, in order:

  1. contextvars set by the caller (the agent-tools bridge endpoint sets
     them from the request; run_v3_agent sets them for in-process runs)
  2. the live pipeline singleton — only one cycle runs at a time
  3. legacy env vars (kept for tests/scripts)
  4. the historical defaults
"""
from __future__ import annotations

import contextvars
import logging
import os
import re

logger = logging.getLogger(__name__)

_cycle_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tool_cycle_id", default=None
)
_agent_name_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tool_agent_name", default=None
)

# Prism forwards its conversation UUID where a trading cycle id belongs;
# never treat one as a cycle.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

_RUNNING_STATUSES = {"starting", "running", "collecting", "analyzing", "trading"}


def normalize_agent_name(name: str | None) -> str | None:
    """CUSTOM_V3_JUNIOR_ANALYST (prism registration) → v3_junior_analyst."""
    if not name:
        return None
    n = name.strip()
    if not n:
        return None
    if n.upper().startswith("CUSTOM_"):
        n = n[len("CUSTOM_"):]
    return n.lower() if n.isupper() else n


def set_tool_context(agent_name: str | None = None, cycle_id: str | None = None) -> None:
    """Record who is executing tools right now (per-async-task)."""
    if agent_name:
        _agent_name_var.set(normalize_agent_name(agent_name))
    if cycle_id and not _UUID_RE.match(cycle_id):
        _cycle_id_var.set(cycle_id)


def clear_tool_context() -> None:
    _agent_name_var.set(None)
    _cycle_id_var.set(None)


def _running_pipeline_cycle_id() -> str | None:
    try:
        from app.services.pipeline_service import PipelineService

        state = getattr(PipelineService, "_state", None) or {}
        if state.get("status") in _RUNNING_STATUSES and state.get("cycle_id"):
            return state["cycle_id"]
    except Exception:
        pass
    return None


def current_cycle_id() -> str:
    ctx = _cycle_id_var.get()
    if ctx:
        return ctx
    live = _running_pipeline_cycle_id()
    if live:
        return live
    env = os.getenv("CYCLE_ID")
    if env:
        return env
    logger.warning(
        "[ToolContext] No cycle context for tool call — falling back to 'default_cycle'"
    )
    return "default_cycle"


def current_agent_name() -> str:
    ctx = _agent_name_var.get()
    if ctx:
        return ctx
    env = normalize_agent_name(os.getenv("AGENT_NAME"))
    return env or "unknown"
