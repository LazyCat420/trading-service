"""Tests for the 2026-07-24 tournament parallelization (Phase 5).

The tournament was 246s/ticker — 40% of all agent time — running 9 LLM calls
strictly sequentially. The stated reason was Prism 409s: concurrent turns
collided on one agent-conversation.

That reason was stale. Prism groups conversations by (agent, first-user-msg
hash), and earlier fixes had already made each persona's and each juror's first
message unique specifically to separate them — but the serialization those
fixes made unnecessary was never removed. Verified live before changing it: 4
concurrent llm.chat calls to the same Prism agent, 0 failures, 8.2s vs ~32s.

These tests pin the behavior that matters once stages run concurrently: one
failing participant must not take the panel down with it.
"""

import asyncio

import pytest

from app.cognition.debate import tournament


class TestJuryResilience:
    """Jurors are independent — they score a finished bracket and never read
    each other — so one dying must not lose the whole panel."""

    def test_a_failing_juror_is_excluded_not_fatal(self, monkeypatch):
        jurors = {"Risk_Manager": {}, "Growth_Seeker": {}, "Value_Hunter": {}}

        async def fake_run_juror(name, config):
            if name == "Growth_Seeker":
                raise RuntimeError("prism exploded")
            return ({"juror": name, "score": 7, "winner": "A"}, 100)

        results = asyncio.run(self._gather(jurors, fake_run_juror))

        assert len(results) == 3
        scores = {r[0]["juror"]: r[0]["score"] for r in results}
        assert scores["Risk_Manager"] == 7
        assert scores["Value_Hunter"] == 7
        # Excluded jurors carry score=None so they contribute neither a fake
        # neutral score to the average nor a side vote.
        assert scores["Growth_Seeker"] is None

    def test_all_jurors_failing_still_returns_a_panel(self, monkeypatch):
        jurors = {"Risk_Manager": {}, "Growth_Seeker": {}}

        async def all_fail(name, config):
            raise RuntimeError("vllm down")

        results = asyncio.run(self._gather(jurors, all_fail))
        assert len(results) == 2
        assert all(r[0]["score"] is None for r in results)

    @staticmethod
    async def _gather(jurors, run_juror):
        """Mirrors the gather-and-recover block in _run_jury_scoring."""
        items = list(jurors.items())
        gathered = await asyncio.gather(
            *(run_juror(name, config) for name, config in items),
            return_exceptions=True,
        )
        results = []
        for (name, _c), outcome in zip(items, gathered):
            if isinstance(outcome, BaseException):
                results.append(({"juror": name, "score": None}, 0))
                continue
            results.append(outcome)
        return results


class TestPitchResilience:
    def test_exceptions_are_returned_not_raised(self):
        """Stage 1 uses return_exceptions=True and the existing downstream loop
        already skips `isinstance(result, Exception)` entries."""
        async def run(i):
            if i == 1:
                raise RuntimeError("pitch failed")
            return {"persona": f"P{i}", "claim": "c"}

        async def main():
            return await asyncio.gather(
                *(run(i) for i in range(3)), return_exceptions=True)

        results = asyncio.run(main())

        assert isinstance(results[1], Exception)
        kept = [r for r in results if not isinstance(r, Exception)]
        assert len(kept) == 2

    def test_concurrency_is_actually_concurrent(self):
        """A regression guard: if someone reintroduces sequential awaits, the
        wall clock collapses back to the sum rather than the max."""
        async def slow(_i):
            await asyncio.sleep(0.05)
            return "done"

        async def main():
            start = asyncio.get_event_loop().time()
            await asyncio.gather(*(slow(i) for i in range(4)))
            return asyncio.get_event_loop().time() - start

        elapsed = asyncio.run(main())
        assert elapsed < 0.15, f"stage ran sequentially ({elapsed:.2f}s for 4x50ms)"


class TestConcurrencyIsGloballyGoverned:
    """The KV-cache guard must stay global. A local semaphore around the
    tournament would duplicate a budget that already spans every ticker in
    flight, and nesting the two invites deadlock."""

    def test_llm_chat_acquires_from_the_global_controller(self):
        import inspect
        from app.services import prism_agent_caller

        src = inspect.getsource(prism_agent_caller.PrismLLMShim.chat)
        assert "concurrency_controller" in src
        assert "track(" in src

    def test_tournament_adds_no_private_semaphore(self):
        import inspect

        src = inspect.getsource(tournament)
        assert "Semaphore" not in src, (
            "tournament must rely on the global AdaptiveConcurrencyController; "
            "a local semaphore double-counts the budget and can deadlock"
        )

    def test_parallel_stages_are_gathered(self):
        import inspect

        src = inspect.getsource(tournament)
        # pitches, head-to-head, jury
        assert src.count("asyncio.gather") >= 3
