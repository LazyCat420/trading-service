"""Boot-time analytics must not pin the event loop the tool bridge runs on.

WHAT HAPPENED. compute_market_regime, compute_sector_breadth,
backfill_sector_performance, compute_sector_performance and
compute_all_correlations are `async def` with ZERO awaits in their bodies —
CPU-bound pandas (pivot, corr, groupby) wearing a coroutine's clothes. Awaiting
one runs it entirely on the event loop.

Measured 2026-08-31, on a cycle launched 40 s after a redeploy: the 5-second
vllm-shim metrics poller stopped for 207 s during sector breadth + cross-asset
correlations, and again for 129 s during the sp500 refresh. `agent_tools_router`
serves the tool bridge from that same loop, so every agent tool call in those
windows returned "bridge timeout after 30000ms": 12 of 62 in
cycle-observe-1788219049, which then overrode BOTH tickers' triage to FULL for
"degraded research". Every one of those requests succeeded the moment the loop
came back — they were queued, not lost. The same message accounts for 98 of 174
tool failures since 2026-08-24, so this is a standing bug, not one bad night.

These tests use a stand-in coroutine with a blocking body rather than the real
analytics: the property under test is "does the loop keep breathing", and the
real functions need a populated price_history to do anything at all.
"""

import ast
import asyncio
import pathlib
import time

import pytest

from app.services.startup_tasks import _off_the_loop

STARTUP = pathlib.Path(__file__).resolve().parents[2] / "app" / "services" / "startup_tasks.py"

BLOCK_S = 0.40
TICK_S = 0.01


async def _blocking_compute():
    """The shape of the real ones: async def, no awaits, CPU-bound body."""
    time.sleep(BLOCK_S)
    return "done"


async def _count_heartbeats(stop: asyncio.Event) -> int:
    """A stand-in for the metrics poller and the tool-bridge route."""
    ticks = 0
    while not stop.is_set():
        await asyncio.sleep(TICK_S)
        ticks += 1
    return ticks


async def _ticks_during(runner) -> tuple[int, str]:
    stop = asyncio.Event()
    beat = asyncio.create_task(_count_heartbeats(stop))
    await asyncio.sleep(0)  # let the heartbeat start
    result = await runner()
    stop.set()
    return await beat, result


class TestTheLoopKeepsBreathing:
    @pytest.mark.asyncio
    async def test_a_direct_await_starves_the_loop(self):
        """The control. Without this, 'it works now' is unfalsifiable — a fast
        machine would pass the fixed case whether or not the fix were present.
        """
        ticks, result = await _ticks_during(_blocking_compute)

        assert result == "done"
        assert ticks <= 1, (
            f"expected the loop to be starved by a direct await, saw {ticks} ticks "
            f"in {BLOCK_S}s — the stand-in is not actually blocking"
        )

    @pytest.mark.asyncio
    async def test_off_the_loop_keeps_it_responsive(self):
        ticks, result = await _ticks_during(
            lambda: _off_the_loop(_blocking_compute, "test")
        )

        assert result == "done"
        assert ticks >= 5, (
            f"only {ticks} heartbeats during a {BLOCK_S}s compute — the work is "
            f"still running on the event loop"
        )

    @pytest.mark.asyncio
    async def test_it_propagates_the_return_value_and_exceptions(self):
        async def _boom():
            raise ValueError("from the worker")

        with pytest.raises(ValueError, match="from the worker"):
            await _off_the_loop(_boom, "boom")


class TestNoBareAwaitsRemain:
    """The guard: a sixth analytic added with a bare await would reopen this."""

    BLOCKING = {
        "compute_market_regime",
        "compute_sector_breadth",
        "compute_all_correlations",
        "backfill_sector_performance",
        "compute_sector_performance",
    }

    def test_every_call_goes_through_the_helper(self):
        tree = ast.parse(STARTUP.read_text(encoding="utf-8"))

        bare = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Await):
                continue
            call = node.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                if call.func.id in self.BLOCKING:
                    bare.append(f"{call.func.id}:{node.lineno}")

        assert not bare, (
            "these CPU-bound coroutines are awaited directly on the event loop, "
            "which starves the tool bridge for as long as the maths takes: "
            + ", ".join(bare)
        )

    def test_the_helper_is_actually_used(self):
        """A guard that finds nothing passes for the wrong reason."""
        src = STARTUP.read_text(encoding="utf-8")

        assert src.count("_off_the_loop(") >= 6
