"""
Step: Memory Context Injection.

Reads prior episodic memories and procedural rules for the ticker,
then injects them as context for the thesis agent.

Extracted from runner.py Step 5.
"""

import asyncio
import logging
import time

from app.ticker_pipeline.context import TickerContext
from app.log_manager import log_manager

logger = logging.getLogger(__name__)


async def run_memory_step(ctx: TickerContext) -> TickerContext:
    """Read prior memories and procedural rules for the ticker."""
    try:
        from app.cognition.memory.reader import read_memories, read_procedural
        from app.pipeline.orchestration.cycle_control import cycle_control

        await cycle_control.wait_if_paused()
        loop = asyncio.get_running_loop()
        prior_episodes, procedural_rules = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: (
                    read_memories(ctx.ticker, memory_types=["episodic"], limit=5),
                    read_procedural(tags=[ctx.ticker.lower(), "all"], limit=10),
                ),
            ),
            timeout=10.0,
        )

        memory_lines: list[str] = []
        for ep in prior_episodes:
            payload = ep.payload or {}
            memory_lines.append(
                f"- [{ep.created_at[:10]}] {payload.get('event_type', 'run')}: "
                f"{payload.get('action', '?')} (conf={ep.confidence:.0f})"
            )
        for rule in procedural_rules:
            memory_lines.append(
                f"- RULE: {rule.rule_text} (conf={rule.confidence:.2f})"
            )

        log_manager.log_v2_cycle(ctx.cycle_id, "v2_memory_read", {
            "ticker": ctx.ticker, "episodes": len(prior_episodes),
            "rules": len(procedural_rules),
        })
        ctx.memory_context = {
            "episode_count": len(prior_episodes),
            "rule_count": len(procedural_rules),
            "memory_brief": (
                "\n".join(memory_lines) if memory_lines else "No prior memory."
            ),
        }
        ctx.add_stage("memory_read")

    except asyncio.TimeoutError:
        logger.warning("[V2] Memory read timed out for %s", ctx.ticker)
        ctx.memory_context = {
            "episode_count": 0, "rule_count": 0,
            "memory_brief": "Memory unavailable (timeout).",
        }
    except Exception as e:
        logger.warning("[V2] Memory read failed for %s (non-fatal): %s", ctx.ticker, e)
        ctx.memory_context = {
            "episode_count": 0, "rule_count": 0,
            "memory_brief": "Memory unavailable.",
        }

    return ctx
