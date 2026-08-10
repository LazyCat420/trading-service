"""Errors raised during a cycle must be filed under that cycle's id.

BACKGROUND
----------
`DbLoggingHandler.emit` resolves the `cycle_id` column of `execution_errors`
and `cycle_audit_log` in this order:

    record.cycle_id  →  get_trace_id()  →  the literal "system-log"

Until 2026-08-10, `set_trace_id` was called in exactly one place in the whole
service (`app/autoresearch/core.py`) and never on the trading-cycle path. The
first two rungs were therefore always empty for cycle work and every warning
and error a cycle produced was filed under `cycle_id = 'system-log'`. Per-cycle
error rates could not be computed at all, which is how four silent failures ran
undetected until 2026-08-10.

These tests pin both halves: the handler's fallback order, and the fact that
the cycle task stamps the id — including across the `create_task` boundary,
which is the property that makes one `set_trace_id` at the top of the cycle
cover every agent beneath it.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

from app.services.logging.unified_logger import DbLoggingHandler
from app.utils.trace import _trace_id_var, get_trace_id, set_trace_id


@pytest.fixture(autouse=True)
def _restore_trace_id():
    """Never leak a trace id out of this file.

    `set_trace_id` writes a module-level ContextVar. A sync test writes it into
    the main thread's context, where it would survive for the rest of the
    session and silently re-attribute another file's log assertions — the same
    order-dependency class as open item 37.
    """
    token = _trace_id_var.set(None)
    try:
        yield
    finally:
        _trace_id_var.reset(token)


def _captured_cycle_ids(handler_calls) -> list[str]:
    """Pull the cycle_id argument out of each INSERT the handler issued."""
    out = []
    for call in handler_calls:
        sql = call.args[0]
        params = call.args[1]
        if "INSERT INTO execution_errors" in sql:
            out.append(params[1])  # (id, cycle_id, phase, ...)
    return out


class TestHandlerFallbackOrder:
    """The three rungs, each proven to be reachable and distinguishable."""

    def _emit(self, mock_db, *, extra=None):
        handler = DbLoggingHandler()
        record = logging.LogRecord(
            name="app.v3.orchestrator", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="boom", args=(), exc_info=None,
        )
        for k, v in (extra or {}).items():
            setattr(record, k, v)
        handler.emit(record)
        return _captured_cycle_ids(mock_db.execute.call_args_list)

    def test_record_attribute_wins(self, mock_db):
        set_trace_id("trace-should-lose")
        assert self._emit(mock_db, extra={"cycle_id": "cycle-v3-explicit"}) == [
            "cycle-v3-explicit"
        ]

    def test_trace_id_is_used_when_record_has_none(self, mock_db):
        set_trace_id("cycle-v3-from-trace")
        assert self._emit(mock_db) == ["cycle-v3-from-trace"]

    def test_system_log_only_when_nothing_is_set(self, mock_db):
        # A fresh context with no trace id — the genuine "no cycle" case.
        def _run():
            return self._emit(mock_db)

        got = contextvars_isolated(_run)
        assert got == ["system-log"], (
            "The fallback must still exist for genuinely cycle-less logging "
            f"(boot, schedulers), got {got!r}"
        )


def contextvars_isolated(fn):
    """Run `fn` in a fresh copy of the context so no earlier set_trace_id leaks."""
    import contextvars

    ctx = contextvars.Context()
    return ctx.run(fn)


class TestTraceIdCrossesTaskBoundaries:
    """One set at the top of the cycle must cover every task spawned beneath it.

    This is the property the fix relies on. `asyncio.create_task` copies the
    current context at creation time, so a set inside the cycle task is
    inherited by every agent task it spawns — and, critically, does NOT leak
    back into the caller that started the cycle.
    """

    async def test_child_tasks_inherit_the_cycle_id(self):
        seen: list[str | None] = []

        async def grandchild():
            seen.append(get_trace_id())

        async def cycle_task():
            set_trace_id("cycle-v3-inherited")
            await asyncio.gather(*(asyncio.create_task(grandchild()) for _ in range(3)))

        await asyncio.create_task(cycle_task())
        assert seen == ["cycle-v3-inherited"] * 3

    async def test_the_caller_does_not_inherit_the_cycle_id(self):
        """The command poller outlives the cycle; it must not keep the id.

        This is why the set lives inside `_run_all_v3` and not in
        `start_cycle`: a set in the caller would survive the cycle and
        mis-attribute every later log line to a finished run.
        """

        async def cycle_task():
            set_trace_id("cycle-v3-scoped")
            assert get_trace_id() == "cycle-v3-scoped"

        assert get_trace_id() is None
        await asyncio.create_task(cycle_task())
        assert get_trace_id() is None, (
            "the cycle's id escaped into the caller's context"
        )

    async def test_to_thread_inherits_the_cycle_id(self):
        """Collectors run under `asyncio.to_thread`; their warnings must attribute."""

        async def cycle_task():
            set_trace_id("cycle-v3-threaded")
            return await asyncio.to_thread(get_trace_id)

        assert await asyncio.create_task(cycle_task()) == "cycle-v3-threaded"


class TestCycleStampsItsId:
    """`_run_all_v3` must set the trace id before it does any work.

    Fails without the `set_trace_id(cycle_id)` at the top of `_run_all_v3`:
    the probe below runs on the first statement inside the cycle's try block,
    so anything logged from that point on would otherwise carry no id.
    """

    async def test_run_all_v3_sets_the_trace_id_before_any_work(self):
        from app.services.pipeline_service import PipelineService

        seen: list[str | None] = []

        class _Boom(RuntimeError):
            pass

        fake_prism = MagicMock()

        def _probe():
            seen.append(get_trace_id())
            raise _Boom("abort the cycle here — we only need the id")

        fake_prism.cleanup_all_sessions.side_effect = _probe

        with patch.dict(
            "sys.modules",
            {"lazycat.llm": MagicMock(prism_client=fake_prism)},
        ), patch.object(PipelineService, "save_state"), patch.object(
            PipelineService, "_state", {}
        ):
            await PipelineService._run_all_v3("cycle-v3-probe", ["AAPL"])

        assert seen == ["cycle-v3-probe"], (
            "The cycle task did not stamp its cycle_id onto the logging "
            f"context; every error it logs would file under 'system-log'. Saw {seen!r}"
        )
