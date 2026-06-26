"""
Unified LLM call wrapper — ALL LLM calls in the pipeline go through here.

Provides:
  - Consistent timeout handling
  - Retry with backoff
  - Token tracking
  - Structured error reporting
  - Agent-name-based routing

Usage:
    from app.core.llm_caller import llm_call, llm_call_with_tools

    text, tokens = await llm_call(
        system="You are a macro analyst...",
        user="Analyze AAPL...",
        agent_name="macro_scout",
        timeout=180,
    )
"""

import asyncio
import logging
import time
from typing import Any

from app.services.prism_agent_caller import llm, Priority

logger = logging.getLogger(__name__)


async def llm_call(
    *,
    system: str,
    user: str,
    agent_name: str = "pipeline",
    timeout: float = 180.0,
    priority: Priority = Priority.NORMAL,
    max_tokens: int | None = None,
    temperature: float | None = None,
    retries: int = 0,
    retry_delay: float = 10.0,
    cycle_id: str = "",
    ticker: str = "",
) -> tuple[str, int]:
    """Make a single LLM call with unified timeout and retry handling.

    Returns:
        (response_text, token_count)

    Raises:
        RuntimeError: If all attempts fail.
    """
    max_attempts = 1 + retries
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                llm.chat(
                    system=system,
                    user=user,
                    agent_name=agent_name,
                    priority=priority,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
                timeout=timeout,
            )
            # llm.chat returns (text, tokens) or just text depending on usage
            if isinstance(result, tuple):
                text, tokens = result[0], result[1]
            else:
                text, tokens = str(result), 0

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if attempt > 0:
                logger.info(
                    "[LLM] %s succeeded on retry %d/%d for %s (%dms)",
                    agent_name, attempt + 1, max_attempts, ticker or "?", elapsed_ms,
                )
            return text, tokens

        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            last_error = asyncio.TimeoutError(
                f"{agent_name} timed out after {timeout}s "
                f"(attempt {attempt + 1}/{max_attempts}, {elapsed_ms}ms)"
            )
            logger.warning(
                "[LLM] TIMEOUT %s for %s (attempt %d/%d, %dms, timeout=%.0fs)",
                agent_name, ticker or "?", attempt + 1, max_attempts, elapsed_ms, timeout,
            )

        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            last_error = e
            logger.warning(
                "[LLM] ERROR %s for %s: %s (attempt %d/%d, %dms)",
                agent_name, ticker or "?", e, attempt + 1, max_attempts, elapsed_ms,
            )

        # Wait before retry (if not the last attempt)
        if attempt < max_attempts - 1:
            logger.info(
                "[LLM] Retrying %s in %.0fs...", agent_name, retry_delay,
            )
            await asyncio.sleep(retry_delay)

    raise RuntimeError(
        f"LLM call failed after {max_attempts} attempts: {last_error}"
    ) from last_error


async def llm_call_with_tools(
    *,
    system: str,
    user: str,
    tools: list[dict],
    agent_name: str = "pipeline",
    timeout: float = 180.0,
    priority: Priority = Priority.NORMAL,
    max_tokens: int | None = None,
    temperature: float | None = None,
    cycle_id: str = "",
    ticker: str = "",
    **kwargs: Any,
) -> tuple[Any, int]:
    """Make a tool-augmented LLM call with unified timeout handling.

    Returns:
        (result, token_count) — result shape depends on llm.chat_with_tools
    """
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(
            llm.chat_with_tools(
                system=system,
                user=user,
                tools=tools,
                agent_name=agent_name,
                priority=priority,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            ),
            timeout=timeout,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.debug(
            "[LLM] %s (tools) completed for %s in %dms",
            agent_name, ticker or "?", elapsed_ms,
        )
        return result

    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        raise RuntimeError(
            f"{agent_name} (tools) timed out after {timeout}s ({elapsed_ms}ms)"
        ) from None
