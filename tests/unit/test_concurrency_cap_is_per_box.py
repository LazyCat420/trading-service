"""The limiter averaged two boxes and let one of them starve.

MEASURED 2026-09-06. Prism's `requests` ledger for GLM on gold-spark
(`dgx_spark`): 16 "Provider stream stalled: no data received for 300s" rows in
three days, and time-to-first-generation for 586 completed rows rising with
in-flight count (p90 42 s alone → 148 s with one neighbour → 235 s with three).
Controlled probes on the idle box (E1a): prefills serialise at ~22 s per 25k
prompt — four in flight reach first token in 31/57/76/91 s, nowhere near 300.
Beside two neighbours mid-DECODE (E1a-2): the same 25k prompt waited 165 s and
161 s for its first token at 38% KV. Prefill is starved by decode.

`AdaptiveConcurrencyController` is alive (12 adjustments in 7 days) but GLOBAL:
`max_capacity=12` is jetson 6 + dgx 6, `running` is summed over both, and the
KV figure it keys on is the AVERAGE across endpoints. The log shows
"Limit adjusted 8 → 6 (cache=35.1%, running=5, waiting=0, max_capacity=12)"
while gold-spark alone carried the five and the Jetson sat at 0%. A cap that
cannot see which box a run will land on cannot protect that box.

Now the controller keeps a pool per box, computes each box's limit from THAT
box's metrics through the same KV tiers, and caps running requests per box at
MAX_RUNNING_PER_BOX. `track()` without a box keeps today's global behaviour.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.services.adaptive_concurrency import (
    MAX_RUNNING_PER_BOX,
    AdaptiveConcurrencyController,
)


@dataclass
class _Ep:
    name: str
    max_concurrent: int
    cache_usage: float
    requests_running: int
    requests_waiting: int
    enabled: bool = True
    model: str | None = "GLM-5.3-Flash-EXL3"


# verbatim shape of the 23:48:23 log line: five running, cache 70% on the box that
# had them, jetson idle — the global average read 35%
DGX_LOADED = _Ep("dgx_spark", 6, 0.70, 5, 0)
JETSON_IDLE = _Ep("jetson", 6, 0.00, 0, 0)


def _controller(monkeypatch, *eps):
    c = AdaptiveConcurrencyController(min_concurrency=1, max_concurrency=8)
    monkeypatch.setattr(c, "_read_endpoints", lambda: list(eps))
    monkeypatch.setattr(c, "_last_eval", 0.0)
    return c


class TestEachBoxIsJudgedOnItsOwnNumbers:
    def test_an_idle_jetson_cannot_raise_the_loaded_boxs_limit(self, monkeypatch):
        c = _controller(monkeypatch, DGX_LOADED, JETSON_IDLE)
        # dgx: 30% remaining -> the existing "<= 40% remaining" tier caps at 2
        assert c.limit_for("dgx_spark") == 2
        # jetson: 100% remaining -> its own cap (the ceiling applies to every
        # box; only gold-spark was measured, so the unmeasured box is not
        # given more than the measured one)
        assert c.limit_for("jetson") == min(6, MAX_RUNNING_PER_BOX)
        assert c.limit_for("jetson") >= c.limit_for("dgx_spark")

    def test_the_global_average_used_to_read_this_as_four(self, monkeypatch):
        """Documents the defect: averaging 0.70 and 0.00 is 0.35 used, 65%
        remaining, which the interpolation tier turns into a limit of 4 —
        for a box that was already at 5 with a full cache."""
        c = _controller(monkeypatch, DGX_LOADED, JETSON_IDLE)
        assert c.current_limit >= 4  # the global figure, unchanged
        assert c.limit_for("dgx_spark") < c.current_limit


class TestTheRunningCeiling:
    def test_a_box_never_gets_more_than_the_ceiling(self, monkeypatch):
        c = _controller(monkeypatch, _Ep("dgx_spark", 6, 0.05, 0, 0))
        assert c.limit_for("dgx_spark") <= MAX_RUNNING_PER_BOX

    def test_the_ceiling_is_inside_the_measured_band(self):
        """E1a-2: two decoders already push a fresh prefill to 160 s; the
        300 s watchdog is the wall. The exact value is set from the full
        E1a-2 curve; it must sit in the band the data supports."""
        assert 2 <= MAX_RUNNING_PER_BOX <= 4


class TestTheOrchestratorNamesTheBox:
    def test_every_v3_agent_run_is_tracked_against_its_box(self):
        import inspect
        from app.v3 import orchestrator
        src = inspect.getsource(orchestrator)
        assert 'box=box_for_agent(' in src, "the orchestrator must name the box or the per-box pool is never used"


class TestPoolsAreIndependent:
    @pytest.mark.asyncio
    async def test_a_full_dgx_pool_does_not_block_a_jetson_run(self, monkeypatch):
        # dgx at 90% remaining -> its cap (the ceiling, 2); fill BOTH slots, so a
        # counter shared across boxes would read "2 of 2" for jetson as well
        c = _controller(monkeypatch, _Ep("dgx_spark", 6, 0.10, 0, 0), JETSON_IDLE)
        acquired = [asyncio.Event() for _ in range(c.limit_for("dgx_spark"))]

        async def hold_dgx(ev):
            async with c.track(label="a", box="dgx_spark"):
                ev.set()
                await asyncio.sleep(0.3)

        tasks = [asyncio.create_task(hold_dgx(ev)) for ev in acquired]
        for ev in acquired:
            await ev.wait()
        # a jetson run must not wait on dgx's pool
        async with asyncio.timeout(0.1):
            async with c.track(label="b", box="jetson"):
                pass
        for t in tasks:
            await t

    @pytest.mark.asyncio
    async def test_a_second_dgx_run_waits_for_the_first(self, monkeypatch):
        c = _controller(monkeypatch, _Ep("dgx_spark", 6, 0.90, 6, 0))
        first = asyncio.Event()

        async def hold():
            async with c.track(label="a", box="dgx_spark"):
                first.set()
                await asyncio.sleep(0.3)

        t = asyncio.create_task(hold())
        await first.wait()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                async with c.track(label="b", box="dgx_spark"):
                    pass
        await t

    @pytest.mark.asyncio
    async def test_track_without_a_box_is_the_global_pool_as_before(self, monkeypatch):
        c = _controller(monkeypatch, DGX_LOADED, JETSON_IDLE)
        async with c.track(label="legacy"):
            assert c.total_active == 1
