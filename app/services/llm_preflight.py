"""LLM pre-flight: prove the model can answer before spending a cycle on it.

MEASURED 2026-08-25 (trading-client ch.95): the vLLM backend was down from
2026-08-21 to 2026-08-24 and **28 cycles ran to completion anyway** — 112
agent runs all failing with "All 5 attempts failed", 51 of 53 analyses
persisted as DEGRADED with confidence 0. Each of those cycles spent full
wall-clock and a watch-desk wake to produce nothing, and no alert fired.

A prism `/health` 200 does not cover this case: prism was reachable while the
model behind it was not. The only honest probe is a minimal completion
through the same route agents use (`chat_toolless` — tool-less `/chat`, so
the probe costs a handful of tokens, not the ~21k-token MCP catalog that
`/agent` attaches server-side).

Fail-open on AMBIGUITY, fail-closed on PROOF. Three verdicts abort: the
endpoint refused N completions, it named a model the decision agents cannot
use (ModelContractError), or it has no servable model at all
(ModelUnavailableError). If the probe machinery itself breaks (import error,
endpoint not configured), the cycle proceeds — a broken probe must not become
the thing that blocks all trading
([[a-cross-target-gate-fails-for-toolchain-reasons]]: a false red costs
authority like a false green).

"Resolver down" is NOT machinery breaking, and reading it that way is what
this module got wrong from 2026-08-28 to 08-30. The resolver reaching the box
and being told there is no model is the same verdict as the completion probe
failing twice — it just arrives one step earlier and costs less.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

PROBE_ATTEMPTS = 2
PROBE_TIMEOUT_S = 25.0
#: Any of the decision agents would do; the point is to resolve the same
#: default model routing a real agent resolves.
PROBE_AGENT_NAME = "v3_decision_synthesizer"


async def llm_can_answer() -> tuple[bool, str]:
    """(ok, detail). ok=False ONLY on positive evidence the LLM path is dead."""
    try:
        from app.services.prism_agent_caller import (
            chat_toolless,
            resolve_default_model_for_agent,
        )

        model, provider = await resolve_default_model_for_agent(PROBE_AGENT_NAME)
    except Exception as exc:
        # Two resolver failures are POSITIVE evidence, not ambiguity.
        #
        # A ModelContractError: the box answered and named a model the
        # decision agents cannot use (the 08-25/26 Qwen3.6 incident killed 45
        # desks this way while this probe's "endpoint alive" check passed).
        #
        # A ModelUnavailableError: we reached the box, asked `/v1/models`
        # twice, got nothing usable, and had no cached id to fall back on.
        # That IS the dead-endpoint verdict this module exists to reach — it
        # arrives one step EARLIER than the completion probe, which is why it
        # used to be misread as our own machinery breaking. Measured
        # 2026-08-28..30: the resolver raised `VLLM endpoint offline: HTTP 502
        # with no usable model list`, this function returned ok=True, and 33
        # desks died at the regime engine (66 calls, 75-102s each) across
        # three days with zero decisions and no page — the exact four-day
        # failure in this module's docstring, recurring one layer up.
        from app.services.prism_agent_caller import (
            ModelContractError,
            ModelUnavailableError,
        )

        if isinstance(exc, ModelContractError):
            logger.error("[llm_preflight] %s", exc)
            return False, f"model contract violated: {exc}"
        if isinstance(exc, ModelUnavailableError):
            logger.error("[llm_preflight] %s", exc)
            return False, f"no servable model: {exc}"
        # Anything else is probe machinery broken — ambiguity, proceed. The
        # config RuntimeErrors beside these two ("endpoint not configured or
        # disabled", "no configured URL") land here on purpose: they say
        # nothing about whether the box is alive.
        logger.warning("[llm_preflight] resolver unavailable (%s) — proceeding", exc)
        return True, f"probe-skipped: resolver unavailable ({exc})"

    last_err = "no attempt ran"
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        try:
            resp = await asyncio.wait_for(
                chat_toolless(
                    provider=provider,
                    model=model,
                    system_prompt="Reply with the single word OK.",
                    user_prompt="Health probe. Reply with the single word OK.",
                    max_tokens=32,
                    timeout_seconds=PROBE_TIMEOUT_S,
                ),
                timeout=PROBE_TIMEOUT_S + 5,
            )
            # The call RETURNED — the endpoint is alive. Do not require
            # non-empty content: a reasoning model can eat a small max_tokens
            # budget entirely and return 200 with an empty body
            # ([[a-reasoning-model-eats-a-small-token-budget-and-returns-200-empty]]),
            # and the 08-21..24 incident's signature was RAISED errors
            # ("All 5 attempts failed"), never empty 200s. Abort only on the
            # signature actually measured.
            content = str((resp or {}).get("content", "")).strip()
            note = "answered" if content else "answered (empty body — endpoint alive)"
            return True, f"model {model} {note} on attempt {attempt}"
        except Exception as exc:  # noqa: BLE001 — each attempt's failure is data
            last_err = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(1.0)

    return False, f"LLM probe failed {PROBE_ATTEMPTS}x via {provider}/{model}: {last_err}"
