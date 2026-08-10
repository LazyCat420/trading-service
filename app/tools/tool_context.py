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
import typing
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_cycle_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tool_cycle_id", default=None
)
_agent_name_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tool_agent_name", default=None
)
# The ticker under analysis. Added 2026-07-29: `tool_usage_stats.ticker` has a
# column but every row was NULL, and a per-ticker pipeline always knows which
# ticker a tool call belongs to — it was simply never carried to the tool layer.
_ticker_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tool_ticker", default=None
)
# The stage of the cycle currently executing. Added 2026-08-10: every row in
# `execution_errors` and `cycle_audit_log` carried phase='unknown' because
# `DbLoggingHandler` reads it from the log record and nothing in app/ ever
# passes extra={"phase": ...}. Unlike cycle_id — set once at the top of the
# cycle and inherited downward — the phase changes many times inside one
# cycle, so it is scoped by `tool_context()` rather than set imperatively.
_phase_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tool_phase", default=None
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


def set_tool_context(
    agent_name: str | None = None,
    cycle_id: str | None = None,
    ticker: str | None = None,
    phase: str | None = None,
) -> None:
    """Record who is executing tools right now (per-async-task).

    Imperative form, kept for callers that have no scope to close: the HTTP
    bridge (`agent_tools_router`) sets the context from request headers and
    returns. Anything with a beginning and an end should use `tool_context()`
    instead, which restores what it replaced.
    """
    if agent_name:
        _agent_name_var.set(normalize_agent_name(agent_name))
    if cycle_id and not _UUID_RE.match(cycle_id):
        _cycle_id_var.set(cycle_id)
    if ticker and ticker.strip():
        _ticker_var.set(ticker.strip().upper())
    if phase and phase.strip():
        _phase_var.set(phase.strip().lower())


def clear_tool_context() -> None:
    _agent_name_var.set(None)
    _cycle_id_var.set(None)
    _ticker_var.set(None)
    _phase_var.set(None)


@contextmanager
def tool_context(
    agent_name: str | None = None,
    cycle_id: str | None = None,
    ticker: str | None = None,
    phase: str | None = None,
):
    """Scope the execution context, and RESTORE the outer values on exit.

    `set_tool_context` cannot be undone. `ContextVar.set` returns a Token that
    `reset()` consumes, and nothing kept one — so `clear_tool_context()` sets
    None, which is not a restore: a nested scope that cleared would blank
    whatever the enclosing scope had established. That was survivable while
    every value was scoped to a whole agent run and the next agent overwrote
    it. A phase is narrower than an agent run, so it is not survivable now.

    Only the arguments actually supplied are pushed; the rest are inherited.
    Tokens are reset in reverse order, including when the body raises.
    """
    tokens: list[tuple[contextvars.ContextVar[typing.Any], contextvars.Token[typing.Any]]] = []
    try:
        if agent_name is not None and str(agent_name).strip():
            norm_agent = normalize_agent_name(agent_name)
            if norm_agent:
                tokens.append((_agent_name_var, _agent_name_var.set(norm_agent)))
        if cycle_id is not None and str(cycle_id).strip():
            cid = str(cycle_id).strip()
            # Same rule as set_tool_context: a Prism conversation UUID is not
            # a cycle, and must not be allowed to masquerade as one.
            if not _UUID_RE.match(cid):
                tokens.append((_cycle_id_var, _cycle_id_var.set(cid)))
        if ticker is not None and str(ticker).strip():
            tokens.append((_ticker_var, _ticker_var.set(str(ticker).strip().upper())))
        if phase is not None and str(phase).strip():
            tokens.append((_phase_var, _phase_var.set(str(phase).strip().lower())))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current_ticker() -> str | None:
    """The ticker under analysis, or None.

    Returns None rather than a sentinel: unlike agent/cycle, a wrong ticker is
    not merely bad telemetry — `get_sec_filings` resolves its subject from this,
    and inventing one would research the wrong company.
    """
    return _ticker_var.get()


def current_cycle_id_or_none() -> str | None:
    """The scoped cycle id, or None — reading the ContextVar and nothing else.

    `current_cycle_id()` below falls back to the live pipeline singleton and to
    env, and WARNS when it finds neither. `DbLoggingHandler` must never call
    it: that warning is itself a log record, which re-enters the handler.
    """
    return _cycle_id_var.get()


def current_phase() -> str | None:
    """The cycle stage currently executing, or None.

    Returns None rather than a sentinel so the caller decides what an unknown
    phase looks like — `DbLoggingHandler` writes 'unknown', which is the value
    that column has always held.
    """
    return _phase_var.get()


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
