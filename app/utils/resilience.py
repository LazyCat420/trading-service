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

from lazycat.resilience import (  # noqa: F401  (re-exported for call sites)
    NON_RETRYABLE_EXCEPTION_NAMES,
    AttemptRecord,
    FailureType,
    ResilientCallError,
    aresilient_call,
    classify_exception,
    resilient_call,
)

# Backwards-compatible alias — this was a private module function before the
# extraction, kept in case anything reached for it.
_classify_exception = classify_exception

# ── App policy ──────────────────────────────────────────────────────────

# DoomLoopException (app/services/streaming_observer.py) signals an agent stuck
# in a repeating tool loop. Retrying it re-enters the same loop, so it must
# abort immediately rather than burn the retry budget.
NON_RETRYABLE_EXCEPTION_NAMES.add("DoomLoopException")

# NOTE on failure telemetry: this module used to try to emit structured retry
# events via `PipelineService.emit(...)`. That method does not exist — the only
# `emit` in pipeline_service.py is a local function nested inside the cycle
# runner — so every call raised AttributeError into a bare `except Exception:
# pass` and no retry event was ever emitted. Rather than port dead code, the
# SDK now exposes `set_failure_emitter(fn)` and nothing is registered here.
# Wiring real telemetry means giving PipelineService an actual class-level
# emit (app/recovery/engine.py:208 calls the same non-existent method) and then
# calling lazycat.resilience.set_failure_emitter() at startup.

__all__ = [
    "FailureType",
    "AttemptRecord",
    "ResilientCallError",
    "aresilient_call",
    "resilient_call",
    "classify_exception",
    "NON_RETRYABLE_EXCEPTION_NAMES",
]
