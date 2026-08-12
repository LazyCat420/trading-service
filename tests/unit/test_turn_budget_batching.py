"""The turn-budget block must tell the model that tool calls can be batched.

WHY THIS TEST EXISTS

Measured over the 30 days to 2026-08-11, tool calls per loop is ~1.0 for every
V3 agent — junior 5.91 calls across 5.95 loops (0.99), quant 0.92, bull 0.73.
Each lookup buys its own LLM round-trip.

That is the whole latency story, because tool execution is not the cost.
Splitting agent wall-clock against summed tool `elapsed_ms` over the same
window: tools are 0.0-11.5% of it (bear 1.5s of 359.2s, quant 0.9s of 272.7s,
junior 18.5s of 160.6s). 88-100% is LLM time. An agent gets faster only by
taking fewer turns.

The budget block already said what SPENDS a turn ("whether you call a tool or
write prose") but never that several tool calls can SHARE one. The harness
honours batching — 436 of 3,163 runs (13.8%) completed more tool calls than
loops, up to 13 more — so this was an unstated affordance, not a platform
limit.

This test pins the instruction to the same block that carries the budget
number, so the two cannot drift apart.
"""
import inspect
import re

from app.v3 import agent_runner


def _budget_block() -> str:
    """The literal source of the tool-enabled turn-budget prompt."""
    src = inspect.getsource(agent_runner)
    start = src.index("### TURN BUDGET:")
    # The block ends at the no-tools branch that follows it.
    end = src.index("You have NO external tools", start)
    return src[start:end]


def test_the_budget_block_still_states_the_number_and_what_spends_a_turn():
    """Guard the pre-existing contract while adding to it."""
    block = _budget_block()
    assert "{_budget} turns" in block
    assert "whether you call a tool or write prose" in block


def test_the_budget_block_tells_the_model_to_batch_independent_calls():
    """The addition. Without this the model pays one turn per lookup."""
    block = _budget_block()
    assert re.search(r"ONE turn, not one each", block), (
        "the budget block must state that several tool calls in a single turn "
        "cost ONE turn. Measured calls/loop is ~1.0 across every agent, and "
        "88-100% of agent wall-clock is LLM time, so batching is the only "
        "lever on latency that the prompt controls."
    )


def test_the_batching_instruction_keeps_its_dependency_caveat():
    """Batching blindly is wrong when one call feeds the next.

    Junior's own prompt asks it to TRACE a lead depth-first — step 3 depends on
    what step 2 surfaced. An instruction to always batch would break that, so
    the exception must survive any future edit to this text.
    """
    block = _budget_block()
    assert re.search(r"depends on", block, re.I), (
        "the batching instruction must exempt calls whose input depends on a "
        "previous call's result, or it will break depth-first tracing"
    )
