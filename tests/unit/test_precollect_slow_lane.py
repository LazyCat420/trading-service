"""The pre-collect slow lane: known-slow collectors get a longer deadline.

cycle-v3-1785504601 (2026-07-31): multi_api_news and youtube hit the 90s
deadline on 6/6 tickers and all 12 completions landed late — work done,
data discarded to the next cycle. Over the prior 7 days the pair missed
the deadline on 59/115 and 67/115 runs, with 90% of late completions
inside ~220s. The fix: after the fast deadline, keep waiting for the
slow pair (only) up to the slow budget. A hung fast collector must not
extend the wait.
"""

import asyncio

from app.v3.data_report import _SLOW_COLLECTORS, wait_with_slow_lane

FAST = 0.05
SLOW = 0.25


async def _sleep_then(seconds: float, value=1):
    await asyncio.sleep(seconds)
    return value


def _tasks(spec: dict[str, float]) -> dict:
    return {asyncio.ensure_future(_sleep_then(s)): name for name, s in spec.items()}


async def test_slow_collector_inside_slow_budget_is_captured():
    tasks = _tasks({"yfinance_price": 0.0, "multi_api_news": FAST * 2})
    pending, budget = await wait_with_slow_lane(tasks, FAST, SLOW)
    assert pending == set()
    assert budget == SLOW  # the extension was applied and must be reported


async def test_slow_collector_beyond_slow_budget_still_times_out():
    tasks = _tasks({"youtube": SLOW * 4})
    pending, budget = await wait_with_slow_lane(tasks, FAST, SLOW)
    assert {tasks[t] for t in pending} == {"youtube"}
    assert budget == SLOW
    for t in tasks:
        t.cancel()


async def test_hung_fast_collector_does_not_extend_the_wait():
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    tasks = _tasks({"reddit": SLOW * 4})  # not in the slow lane
    pending, budget = await wait_with_slow_lane(tasks, FAST, SLOW)
    elapsed = loop.time() - t0
    assert {tasks[t] for t in pending} == {"reddit"}
    assert budget == FAST
    assert elapsed < SLOW  # cut at the fast deadline, no extension
    for t in tasks:
        t.cancel()


async def test_everything_on_time_uses_the_fast_budget():
    tasks = _tasks({"yfinance_price": 0.0, "multi_api_news": 0.0})
    pending, budget = await wait_with_slow_lane(tasks, FAST, SLOW)
    assert pending == set()
    assert budget == FAST


async def test_fast_straggler_finishing_during_extension_is_not_timed_out():
    """The extension only waits ON the slow pair, but pending is recomputed
    from task state — a fast collector that happens to finish in that window
    delivered its data before the report was built, so it must not be
    reported as timed out."""
    tasks = _tasks({"reddit": FAST * 2, "youtube": FAST * 3})
    pending, budget = await wait_with_slow_lane(tasks, FAST, SLOW)
    assert pending == set()
    assert budget == SLOW


def test_slow_lane_covers_exactly_the_measured_offenders():
    assert _SLOW_COLLECTORS == {"multi_api_news", "youtube"}
