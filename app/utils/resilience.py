"""
Resilience decorators — thin app-side binding over lazycat.resilience.

The retry/backoff machinery itself was extracted to the SDK
(lazycat/resilience.py) so every service in this ecosystem shares one
implementation. This module stays as the app's import surface — all existing
`from app.utils.resilience import ...` call sites keep working — and supplies
the two pieces of policy that are specific to this application.

Usage is unchanged:
    from app.utils.resilience import aresilient_call, resilient_call

    @aresilient_call(retries=3, backoff="exponential")
    async def call_llm(...):
        ...
"""

import os

from lazycat.resilience import (  # noqa: F401  (re-exported for call sites)
    NON_RETRYABLE_EXCEPTION_NAMES,
    AttemptRecord,
    FailureType,
    ResilientCallError,
    aresilient_call,
    classify_exception,
    resilient_call,
    set_failure_emitter,
)

# Backwards-compatible alias — this was a private module function before the
# extraction, kept in case anything reached for it.
_classify_exception = classify_exception

# ── App policy ──────────────────────────────────────────────────────────

# DoomLoopException (app/services/streaming_observer.py) signals an agent stuck
# in a repeating tool loop. Retrying it re-enters the same loop, so it must
# abort immediately rather than burn the retry budget.
NON_RETRYABLE_EXCEPTION_NAMES.add("DoomLoopException")

# ── Failure telemetry ───────────────────────────────────────────────────

# Emit one event per *give-up* rather than per failed attempt. A single
# exhausted call at retries=5 (base_agent.py) would otherwise write five rows
# into pipeline_events, multiplied by agents x tickers x cycles — the interim
# attempts are already in the logs, and the actionable event is the one where
# the call stopped trying. Set RESILIENCE_EMIT_EVERY_ATTEMPT=true to get all of
# them back while debugging a flapping upstream.
_EMIT_EVERY_ATTEMPT = os.getenv("RESILIENCE_EMIT_EVERY_ATTEMPT", "false").lower() in (
    "1",
    "true",
    "yes",
)


def _pipeline_emit(
    func_name: str,
    attempt: int,
    max_attempts: int,
    failure_type: FailureType,
    exc: Exception,
    elapsed_ms: int,
    final: bool = False,
) -> None:
    """Put a retry failure on the current cycle's event stream.

    Registered with the SDK below. Import is deliberately lazy —
    pipeline_service imports the v3 orchestrator, which reaches back into this
    module, so a module-level import here is circular.

    `final` (from the SDK) is True on any give-up — budget exhausted OR an
    early stop like DoomLoopException. Gate on it rather than `attempt <
    max_attempts`, which silently dropped early stops (an early give-up has
    attempt < max_attempts yet is still terminal).
    """
    if not _EMIT_EVERY_ATTEMPT and not final:
        return

    from app.services.pipeline_service import PipelineService

    PipelineService.emit(
        "recovery",
        f"retry_{func_name}",
        f"Attempt {attempt}/{max_attempts} failed: {type(exc).__name__}: "
        f"{str(exc)[:100]} [{failure_type.value}]",
        # A give-up (final) is an error; an interim attempt — only reachable
        # under RESILIENCE_EMIT_EVERY_ATTEMPT — is a warning.
        status="error" if final else "warning",
        data={
            "func": func_name,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "failure_type": failure_type.value,
            "final": final,
            "error_type": type(exc).__name__,
            "error_msg": str(exc)[:200],
            "elapsed_ms": elapsed_ms,
        },
        elapsed_ms=elapsed_ms,
    )


set_failure_emitter(_pipeline_emit)

# (The former "early stop produces no event" gap is closed as of
# lazycat-sdk 0.3.1 — the SDK now emits from its stop branch with final=True,
# and this emitter gates on `final` instead of the attempt count.)

__all__ = [
    "FailureType",
    "AttemptRecord",
    "ResilientCallError",
    "aresilient_call",
    "resilient_call",
    "classify_exception",
    "NON_RETRYABLE_EXCEPTION_NAMES",
    "set_failure_emitter",
]
