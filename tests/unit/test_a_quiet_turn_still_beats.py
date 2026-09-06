"""The tool-result heartbeat cannot cover a turn that calls no tool.

`PipelineStateDB.heartbeat` rides on `record_tool_call`, which was the right
seam for the gaps that existed when it was written — all four >300 s gaps in
cycle-v3-1788682529 contained tool calls. It is not enough. OBSERVED on the
first observed verification cycle, cycle-v3-1788719122: one gap of **392 s**
(18:50:04 → 18:56:36) inside `v3_bear_agent`, which made three tool calls
early and then generated for six minutes with nothing to stamp. The debate
agents are the quiet ones — the latency audit measured bear_agent at 2.3 s of
tool time against a 460 s mean run.

So the beat belongs where the run is awaited, not where its tools report: one
task per agent run, stamping on the same throttle, cancelled when the run
returns. `PipelineStateDB.heartbeat` is already throttled and scoped to the
cycle, so the two callers cannot double-write.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.v3 import agent_runner


class TestTheBeatWhileARunIsInFlight:
    @pytest.mark.asyncio
    async def test_a_quiet_run_is_still_stamped(self):
        beats: list[str] = []

        async def _quiet_run():
            await asyncio.sleep(0.25)
            return "done"

        with patch.object(agent_runner, "_HEARTBEAT_INTERVAL_S", 0.05), \
             patch("app.services.pipeline_state.PipelineStateDB.heartbeat",
                   side_effect=lambda cid: beats.append(cid)):
            out = await agent_runner._with_heartbeat(_quiet_run(), "cycle-v3-x")

        assert out == "done"
        assert len(beats) >= 3, (
            f"a quarter-second run produced {len(beats)} beats at a 50 ms "
            "interval — a six-minute silent turn would produce none"
        )
        assert set(beats) == {"cycle-v3-x"}

    @pytest.mark.asyncio
    async def test_the_beat_stops_when_the_run_does(self):
        beats: list[str] = []

        async def _quick():
            return 1

        with patch.object(agent_runner, "_HEARTBEAT_INTERVAL_S", 0.01), \
             patch("app.services.pipeline_state.PipelineStateDB.heartbeat",
                   side_effect=lambda cid: beats.append(cid)):
            await agent_runner._with_heartbeat(_quick(), "cycle-v3-x")
            before = len(beats)
            await asyncio.sleep(0.05)

        assert len(beats) == before, "the heartbeat outlived its run"

    @pytest.mark.asyncio
    async def test_an_exception_still_propagates_and_stops_the_beat(self):
        beats: list[str] = []

        async def _boom():
            await asyncio.sleep(0.02)
            raise RuntimeError("agent died")

        with patch.object(agent_runner, "_HEARTBEAT_INTERVAL_S", 0.005), \
             patch("app.services.pipeline_state.PipelineStateDB.heartbeat",
                   side_effect=lambda cid: beats.append(cid)):
            with pytest.raises(RuntimeError, match="agent died"):
                await agent_runner._with_heartbeat(_boom(), "cycle-v3-x")
            before = len(beats)
            await asyncio.sleep(0.03)

        assert len(beats) == before

    @pytest.mark.asyncio
    async def test_a_cancelled_run_is_still_cancelled(self):
        """asyncio.wait_for cancels the awaitable on timeout; wrapping it must
        not swallow that — the timeout path owns the partial-cost row."""
        async def _forever():
            await asyncio.sleep(10)

        with patch.object(agent_runner, "_HEARTBEAT_INTERVAL_S", 0.01), \
             patch("app.services.pipeline_state.PipelineStateDB.heartbeat"):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    agent_runner._with_heartbeat(_forever(), "cycle-v3-x"),
                    timeout=0.05,
                )

    @pytest.mark.asyncio
    async def test_one_failed_stamp_does_not_end_the_beat(self):
        """The property that is actually observable.

        "A failing stamp cannot break the run" is true however the beat is
        written — it lives in its own task, so its exception never reaches the
        awaited run, and a test asserting that passes with or without the
        guard. What the guard really buys is that the beat SURVIVES: one bad
        stamp must not kill the task and silence the rest of a long turn.
        """
        calls: list[str] = []

        def _flaky(cid):
            calls.append(cid)
            if len(calls) == 1:
                raise RuntimeError("mongo blip")

        async def _run():
            await asyncio.sleep(0.06)
            return "ok"

        with patch.object(agent_runner, "_HEARTBEAT_INTERVAL_S", 0.005), \
             patch("app.services.pipeline_state.PipelineStateDB.heartbeat",
                   side_effect=_flaky):
            assert await agent_runner._with_heartbeat(_run(), "cycle-v3-x") == "ok"

        assert len(calls) > 1, (
            f"the beat stopped after its first failed stamp ({len(calls)} call) "
            "— a store blip would silence the rest of the turn"
        )

    def test_the_interval_matches_the_stores_own_throttle(self):
        from app.services.pipeline_state import PipelineStateDB

        assert agent_runner._HEARTBEAT_INTERVAL_S == PipelineStateDB.HEARTBEAT_MIN_INTERVAL_S, (
            "beating faster than the store's throttle only burns calls; "
            "slower leaves a gap the throttle would have allowed"
        )

    def test_the_runner_wraps_the_agent_await(self):
        """Source-level: the wrapper is useless if the run is not inside it."""
        import inspect

        src = inspect.getsource(agent_runner)
        assert src.count("_with_heartbeat(") >= 3, (
            "both run_agent awaits (the main call and the repair call) must be "
            "wrapped, or the quiet turn they cover is still silent"
        )
