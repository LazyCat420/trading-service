"""Async helpers that preserve execution context across thread boundaries.

`loop.run_in_executor(None, fn)` hands `fn` to a `ThreadPoolExecutor` worker,
and a worker thread does NOT inherit the caller's `contextvars`. Every warning
or error logged inside one is therefore attributed to nothing, however
carefully the async side was scoped: `DbLoggingHandler` reads cycle/phase/ticker
from context variables that are empty in that thread, and the row lands under
'system-log' / 'unknown' / 'system'.

There were six bare call sites when this was written (three collectors, the
pipeline's event appender, and both scraper search collectors). The scraper
pair matter most — a DuckDuckGo search behind a bot wall is the most common
refusal in this system, and those are exactly the warnings worth attributing.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from concurrent.futures import Executor
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def submit_with_context(
    func: Callable[..., T],
    *args: Any,
    executor: Executor | None = None,
) -> "asyncio.Future[T]":
    """Schedule `func` on the executor with the caller's context, return the future.

    The context is copied at the CALL SITE, not inside the worker — by the time
    the worker runs there is nothing left to copy from. Use this where the
    result is deliberately not awaited (fire-and-forget telemetry); use
    `run_in_executor_with_context` everywhere else.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return loop.run_in_executor(
        executor,
        ctx.run,
        functools.partial(func, *args),
    )


async def run_in_executor_with_context(
    func: Callable[..., T],
    *args: Any,
    executor: Executor | None = None,
) -> T:
    """`loop.run_in_executor`, with the caller's context copied into the worker."""
    return await submit_with_context(func, *args, executor=executor)
