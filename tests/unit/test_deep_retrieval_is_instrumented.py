"""The Board -> Synthesizer gap must be visible while it is happening.

MEASURED 2026-09-05/06 (Appendix K.8 of the trading-cycle audit).

SNOW's event stream:  board_of_directors_done 23:44:50 -> decision_synthesizer
starting 23:48:05. **195 seconds of silence.** LULU, previous cycle: 181 s.
`pipeline_state.progress` sat on the Board's chat line the whole time, which is
the same "is it stuck?" shape the operator reported at 10:44.

The container log fills the gap: five `retrieval_hybrid` calls at 23:48:03-05,
then `[decompose] SNOW: 5 sub-queries -> 8 unique chunks`, then `deep retrieval
injected for synthesizer (verdict confidence 58)`. When the debate judge's
confidence is under 60, `orchestrator` calls `build_decomposed_block`, which
makes **one LLM call** to split a fixed question into sub-queries and then runs
five hybrid retrievals. The retrievals took ~2 s; the other ~190 s was the LLM.

That call is invisible to everything this repo aggregates: it is not an agent,
so there is no `v3_agent_telemetry` row, no `agent_start`/`agent_done` event and
no `[V3Runner]` line — and the synthesizer's own "starting..." emit happens
AFTER it returns. Across 16 days the board->synthesizer gap has a median of 0 s
on DeepSeek (n=52) and Nemotron (n=31) because the gate mostly stays shut; on
GLM it is n=4, median 160 s, every desk.

The gate is a coin flip two points from the judge's favourite number: over 105
desks the modal sub-60 confidence is **58 on every model** (DeepSeek 16 of its
25 sub-60 verdicts; both of GLM's).
"""
from __future__ import annotations

import asyncio

import pytest


class _Recorder:
    """Stands in for the orchestrator's `emit`."""

    def __init__(self):
        self.events: list[tuple] = []

    def __call__(self, phase, step, detail, status="running", data=None):
        self.events.append((phase, step, detail, status, data))

    def steps(self) -> list[str]:
        return [e[1] for e in self.events]


@pytest.fixture
def emit():
    return _Recorder()


async def _run(emit, *, ticker="SNOW", confidence=58, block="## DEEP RECALL\n...",
               delay=0.05, boom=False):
    from app.v3 import orchestrator

    async def _fake_block(tkr, question):
        await asyncio.sleep(delay)
        if boom:
            raise RuntimeError("retrieval is down")
        return block

    return await orchestrator.run_deep_retrieval_for_synthesizer(
        ticker=ticker,
        judge_confidence=confidence,
        emit=emit,
        build_block=_fake_block,
    )


class TestTheGapIsAnnounced:
    @pytest.mark.asyncio
    async def test_a_start_and_a_done_event_bracket_the_call(self, emit):
        await _run(emit)

        steps = emit.steps()
        assert steps == [
            "v3_deep_retrieval_SNOW",
            "v3_deep_retrieval_done_SNOW",
        ], f"expected a start and a done event, got {steps}"

    @pytest.mark.asyncio
    async def test_the_start_event_says_why_it_is_running(self, emit):
        """An operator watching the panel needs the reason, not just a spinner:
        this only happens when the debate verdict is weak."""
        await _run(emit, confidence=58)

        _, _, detail, status, _ = emit.events[0]
        assert "58" in detail
        assert status == "running"

    @pytest.mark.asyncio
    async def test_the_done_event_carries_how_long_it_took(self, emit):
        await _run(emit, delay=0.05)

        _, _, detail, status, data = emit.events[-1]
        assert status == "ok"
        elapsed = (data or {}).get("elapsed_ms")
        assert isinstance(elapsed, int)
        assert elapsed >= 40, f"elapsed_ms={elapsed} did not measure the call"

    @pytest.mark.asyncio
    async def test_the_block_is_returned_for_the_desk(self, emit):
        block = await _run(emit, block="## DEEP RECALL\nchunk")
        assert block == "## DEEP RECALL\nchunk"


class TestTheGateStaysShut:
    @pytest.mark.asyncio
    async def test_a_confident_verdict_skips_the_call_and_emits_nothing(self, emit):
        """60 and above must not pay for it, and must not clutter the stream."""
        block = await _run(emit, confidence=60)

        assert block is None
        assert emit.steps() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("confidence", [0, 59])
    async def test_the_threshold_boundary_is_below_sixty(self, emit, confidence):
        await _run(emit, confidence=confidence)
        assert emit.steps(), f"confidence {confidence} should have run it"


class TestItCannotBreakTheCycle:
    @pytest.mark.asyncio
    async def test_a_failed_retrieval_is_reported_and_swallowed(self, emit):
        """Non-fatal by design: the synthesizer runs without the block. But a
        failure that emits nothing is how this became invisible in the first
        place."""
        block = await _run(emit, boom=True)

        assert block is None
        steps = emit.steps()
        assert steps[0] == "v3_deep_retrieval_SNOW"
        assert steps[-1] == "v3_deep_retrieval_done_SNOW"
        assert emit.events[-1][3] == "error"

    @pytest.mark.asyncio
    async def test_an_empty_block_is_not_reported_as_a_success_with_content(self, emit):
        block = await _run(emit, block="")

        assert block in (None, "")
        assert emit.events[-1][3] in ("ok", "warn")


class TestTheOrchestratorUsesIt:
    def test_the_final_decision_branch_calls_the_helper(self):
        """The seam. `build_decomposed_block` must no longer be awaited inline
        in the whiteboard subscriber, or the events never fire in production.
        """
        import ast
        import inspect
        import pathlib

        from app.v3 import orchestrator

        tree = ast.parse(
            pathlib.Path(inspect.getsourcefile(orchestrator)).read_text()
        )

        inline = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "build_decomposed_block"
        ]
        assert not inline, (
            "build_decomposed_block is still awaited inline at line(s) "
            f"{inline} — that call is the 195-second unannounced gap"
        )

        assert hasattr(orchestrator, "run_deep_retrieval_for_synthesizer")
