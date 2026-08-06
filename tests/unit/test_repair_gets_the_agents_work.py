"""The artifact repair pass must be given the agent's own research.

2026-08-05: the dominant artifact failure is an agent that narrates its next
step and runs out of turns — "I'll complete the analysis and emit the desk_note
JSON" (108 chars) — so the text handed to the repair contains no analysis at
all. Repairing from that alone asks the model to write a report out of nothing.
"""

import inspect

from app.agents import base_agent
from app.v3 import agent_runner


def test_run_agent_returns_a_tool_transcript():
    """Without this key the repair has nothing but the last sentence."""
    src = inspect.getsource(base_agent)
    assert '"tool_transcript": tool_transcript' in src


def test_the_transcript_is_bounded():
    """It rides in a prompt that is already ~27k chars."""
    assert base_agent._TRANSCRIPT_MAX_ENTRIES <= 20
    assert base_agent._TRANSCRIPT_ENTRY_CHARS <= 4000
    worst_case = base_agent._TRANSCRIPT_MAX_ENTRIES * base_agent._TRANSCRIPT_ENTRY_CHARS
    assert worst_case <= 40_000, f"worst-case transcript {worst_case} chars is too large"


def test_the_transcript_resets_between_retries():
    """A repair must be built from the attempt that actually failed."""
    assert "tool_transcript.clear()" in inspect.getsource(base_agent)


def test_the_repair_prompt_consumes_the_transcript():
    src = inspect.getsource(agent_runner)
    assert 'result.get("tool_transcript")' in src
    assert "WHAT YOU ALREADY FOUND" in src
    # …and it must still carry the failed attempt, so the model can see what
    # shape was rejected.
    assert "PREVIOUS ATTEMPT (UNPARSEABLE)" in src


def test_transcript_is_declared_outside_the_retry_wrapper():
    """Declared inside _agent_llm_call it would be invisible at the return —
    the bug this test exists to prevent recurring."""
    src = inspect.getsource(base_agent)
    decl = src.index("tool_transcript: list[dict] = []")
    call = src.index("async def _agent_llm_call")
    assert decl < call, "tool_transcript must be declared before _agent_llm_call"
