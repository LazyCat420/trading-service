"""A thread-pool worker must inherit the caller's attribution context.

`loop.run_in_executor(None, fn)` runs `fn` in a `ThreadPoolExecutor` worker,
and worker threads do NOT inherit `contextvars`. Every warning logged inside
one is therefore attributed to nothing — `DbLoggingHandler` reads cycle, phase
and ticker from context variables that are empty in that thread, and the row
lands under 'system-log' / 'unknown' / 'system'.

THE FIRST TEST HERE ASSERTS THE BROKEN FORM IS BROKEN, on purpose. A
propagation test that passes against the bare `run_in_executor` is not testing
propagation — the context would have to be arriving from somewhere else for it
to pass, which is the bug it exists to catch. Both directions, same file.
"""

from __future__ import annotations

import asyncio

import pytest

from app.tools.tool_context import (
    current_cycle_id_or_none,
    current_phase,
    current_ticker,
    tool_context,
)
from app.utils.async_utils import run_in_executor_with_context, submit_with_context


def _read_context() -> tuple[str | None, str | None, str | None]:
    """Executed INSIDE the worker thread."""
    return current_cycle_id_or_none(), current_phase(), current_ticker()


async def test_bare_run_in_executor_loses_the_context():
    """The negative control. If this ever passes, the positive test is worthless."""
    with tool_context(cycle_id="cycle-neg-1", ticker="AAPL", phase="precollect"):
        loop = asyncio.get_running_loop()
        seen = await loop.run_in_executor(None, _read_context)

    assert seen == (None, None, None), (
        "a bare run_in_executor worker saw context it cannot inherit — the "
        "positive test below would then prove nothing"
    )


async def test_wrapped_executor_carries_all_three():
    with tool_context(cycle_id="cycle-pos-1", ticker="msft", phase="Junior_Analyst"):
        seen = await run_in_executor_with_context(_read_context)

    assert seen == ("cycle-pos-1", "junior_analyst", "MSFT")


async def test_wrapped_executor_forwards_arguments():
    def _add(a: int, b: int) -> int:
        return a + b

    assert await run_in_executor_with_context(_add, 2, 3) == 5


async def test_submit_with_context_is_the_fire_and_forget_variant():
    """`pipeline_service` schedules its event append without awaiting it."""
    box: list[tuple] = []

    def _record() -> None:
        box.append(_read_context())

    with tool_context(cycle_id="cycle-pos-2", ticker="NVDA", phase="trading"):
        fut = submit_with_context(_record)
    await fut

    assert box == [("cycle-pos-2", "trading", "NVDA")]


async def test_two_concurrent_scopes_do_not_cross():
    """AAPL/freshness_gate and MSFT/analyzing run at once and stay separate."""

    async def one(cycle: str, ticker: str, phase: str):
        with tool_context(cycle_id=cycle, ticker=ticker, phase=phase):
            # Yield control so the two tasks genuinely interleave rather than
            # running to completion one after the other.
            await asyncio.sleep(0)
            return await run_in_executor_with_context(_read_context)

    a, b = await asyncio.gather(
        one("cycle-a", "AAPL", "freshness_gate"),
        one("cycle-b", "MSFT", "analyzing"),
    )

    assert a == ("cycle-a", "freshness_gate", "AAPL")
    assert b == ("cycle-b", "analyzing", "MSFT")


def test_no_bare_run_in_executor_survives_in_app():
    """The six sites are converted; a seventh must not appear unnoticed.

    Checked against the source rather than by running it, because a collector
    that only makes a bare call on its error path would otherwise pass every
    behavioural test in this file.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "run_in_executor(" in line and "def run_in_executor" not in line:
                offenders.append(f"{path.relative_to(root.parent)}:{n}")

    # app/utils/async_utils.py is the one legitimate caller — it is the wrapper.
    offenders = [o for o in offenders if not o.startswith("app/utils/async_utils.py")]
    assert not offenders, (
        "bare loop.run_in_executor drops the attribution context; use "
        f"app.utils.async_utils.run_in_executor_with_context — found: {offenders}"
    )
