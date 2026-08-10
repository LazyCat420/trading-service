"""A stopped cycle must sever its LLM calls, and the next cycle must un-sever them.

WHY
---
`PrismLLMShim` carries a `_killed` flag. `chat()` and `chat_with_tools()` raise
`CancelledError` while it is set, which is how STOP actually stops a cycle:
without it, every in-flight agent keeps streaming against the shared Jetson and
Gold Spark long after the user pressed stop, burning tokens on a run whose
result is already discarded.

The flag is only useful if two pieces of wiring hold, and until 2026-08-10
**neither had a single test**:

- `PipelineService.request_stop()` must ARM it, or stop is cosmetic.
- `PipelineService.start_cycle()` must RESET it, or the first cycle after any
  stop dies on its first LLM call — the failure is a `CancelledError` from
  inside the agent loop, which reads as a cancelled cycle rather than a stale
  flag, so it is expensive to diagnose from the outside.

A 2026-08-10 plan proposed testing this in `app/services/vllm_client.py`. That
file does not exist; the kill switch lives in `app/services/prism_agent_caller.py`
and has since the vLLM client was removed.

These are pure in-process tests: no network, no database, no cycle.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.prism_agent_caller import llm


class _RaisesOnAccess:
    """A stand-in for the first object `chat()` touches after the flag check.

    Reaching it proves the call got PAST the kill switch, without any network.
    """

    def __getattr__(self, name):
        raise RuntimeError("got past the kill-switch check")


@pytest.fixture(autouse=True)
def _restore_kill_switch():
    """Never leave the shared singleton armed for the rest of the suite."""
    before = getattr(llm, "_killed", False)
    try:
        yield
    finally:
        llm._killed = before


class TestTheFlagItself:
    async def test_armed_switch_refuses_a_chat(self):
        await llm.abort_active_requests()
        with pytest.raises(asyncio.CancelledError):
            await llm.chat(system="s", user="u")

    async def test_reset_lets_calls_through_again(self):
        """The reset must be observable, not just assumed.

        Asserting only that `_killed` flipped would pass even if `chat()` had
        stopped reading the flag. This asserts the gate re-opens, by getting
        past it — the call is stubbed at the first thing `chat` does after the
        check, so nothing leaves the process.
        """
        await llm.abort_active_requests()
        llm.reset_kill_switch()

        # `chat()` calls start_metrics_polling() BEFORE it checks the flag, so
        # the probe has to sit on the first thing AFTER the check — otherwise
        # both states raise and the test measures nothing.
        with patch.object(llm, "start_metrics_polling", MagicMock()), patch(
            "app.services.adaptive_concurrency.concurrency_controller",
            _RaisesOnAccess(),
        ):
            with pytest.raises(RuntimeError, match="got past"):
                await llm.chat(system="s", user="u")

    async def test_the_check_discriminates(self):
        """Armed and reset must produce DIFFERENT outcomes.

        A gate that raises in both states, or neither, is not a gate.
        """
        with patch.object(llm, "start_metrics_polling", MagicMock()), patch(
            "app.services.adaptive_concurrency.concurrency_controller",
            _RaisesOnAccess(),
        ):
            await llm.abort_active_requests()
            with pytest.raises(asyncio.CancelledError):
                await llm.chat(system="s", user="u")

            llm.reset_kill_switch()
            with pytest.raises(RuntimeError, match="got past"):
                await llm.chat(system="s", user="u")


class TestPipelineWiring:
    async def test_request_stop_arms_the_switch(self):
        """Async on purpose: `request_stop` arms the shim via
        `loop.create_task(llm.abort_active_requests())`, and with no running
        loop that whole branch is swallowed by its own `except RuntimeError`.
        A sync test here would report the switch un-armed and blame the code."""
        from app.services.pipeline_service import PipelineService

        llm.reset_kill_switch()
        with patch.object(PipelineService, "save_state"), patch.object(
            PipelineService, "_state", {}
        ):
            PipelineService.request_stop()
            await asyncio.sleep(0)  # let the create_task'd abort actually run

        assert getattr(llm, "_killed", False) is True, (
            "request_stop() did not arm the kill switch — every in-flight agent "
            "keeps streaming against the shared boxes after the user pressed stop"
        )

    async def test_start_cycle_resets_the_switch(self):
        """Pinned because the failure mode is silent and expensive.

        A stale armed flag makes the next cycle die on its first LLM call with a
        `CancelledError` raised from inside the agent loop, which reads as "the
        cycle was cancelled" rather than "the switch was never reset".
        """
        from app.services.pipeline_service import PipelineService

        await llm.abort_active_requests()
        assert getattr(llm, "_killed", False) is True, "precondition failed"

        db_state = {"status": "idle"}
        with patch(
            "app.services.pipeline_state.PipelineStateDB.get_state",
            return_value=db_state,
        ), patch.object(PipelineService, "save_state"), patch.object(
            PipelineService, "_state", {}
        ), patch.object(
            PipelineService, "_cycle_task", None
        ), patch(
            "app.services.pipeline_service.resolve_tickers_batch", lambda t: t
        ), patch(
            "asyncio.create_task", MagicMock()
        ):
            await PipelineService.start_cycle(["AAPL"], cycle_id="test-reset")

        assert getattr(llm, "_killed", False) is False, (
            "start_cycle() left the kill switch armed — the new cycle's first "
            "LLM call will raise CancelledError from inside the agent loop"
        )
