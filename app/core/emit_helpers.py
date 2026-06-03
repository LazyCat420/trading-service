"""
Shared emit helpers — standardized event emission patterns.

Provides pre-built emit wrappers so phase files don't each reinvent
their own emit formatting. Every emit call in the pipeline should use
one of these helpers instead of raw emit() with inline string formatting.

Usage:
    from app.core.emit_helpers import emit_step_start, emit_step_done, emit_step_error

    emit_step_start(emit, "analyzing", ticker, "Building evidence packet...")
    emit_step_done(emit, "analyzing", ticker, "Evidence: 42 claims", elapsed_ms=1234)
    emit_step_error(emit, "analyzing", ticker, "Evidence build failed", error=str(e))
"""

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def noop_emit(*args: Any, **kwargs: Any) -> None:
    """No-op emit for when no emit callback is provided."""
    pass


def emit_step_start(
    emit: Callable,
    phase: str,
    ticker: str,
    message: str,
    *,
    step_name: str = "",
    data: dict | None = None,
) -> None:
    """Emit a step-starting event."""
    step = step_name or f"v2_start_{ticker}"
    emit(
        phase,
        step,
        f"{ticker}: {message}",
        status="running",
        data=data,
    )


def emit_step_done(
    emit: Callable,
    phase: str,
    ticker: str,
    message: str,
    *,
    step_name: str = "",
    elapsed_ms: int = 0,
    data: dict | None = None,
) -> None:
    """Emit a step-completed event."""
    step = step_name or f"v2_done_{ticker}"
    kwargs: dict[str, Any] = {"status": "ok"}
    if elapsed_ms:
        kwargs["elapsed_ms"] = elapsed_ms
    if data:
        kwargs["data"] = data
    emit(phase, step, f"{ticker}: {message}", **kwargs)


def emit_step_error(
    emit: Callable,
    phase: str,
    ticker: str,
    message: str,
    *,
    step_name: str = "",
    error: str = "",
    data: dict | None = None,
) -> None:
    """Emit a step-failed event."""
    step = step_name or f"v2_error_{ticker}"
    full_msg = f"{ticker}: {message}"
    if error:
        full_msg += f" — {error}"
    emit(
        phase,
        step,
        full_msg,
        status="error",
        data=data,
    )


def emit_step_warning(
    emit: Callable,
    phase: str,
    ticker: str,
    message: str,
    *,
    step_name: str = "",
    data: dict | None = None,
) -> None:
    """Emit a step-warning event."""
    step = step_name or f"v2_warning_{ticker}"
    emit(
        phase,
        step,
        f"{ticker}: {message}",
        status="warning",
        data=data,
    )


def emit_decision(
    emit: Callable,
    ticker: str,
    action: str,
    confidence: int,
    elapsed_s: float,
    total_tokens: int,
    *,
    rationale: str = "",
) -> None:
    """Emit the final decision event for a ticker."""
    emoji = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "🟡"
    emit(
        "analyzing",
        f"v2_decision_{ticker}",
        f"{emoji} {ticker}: {action} @ {confidence}% "
        f"| V2 cognition | {elapsed_s:.1f}s"
        f" | {total_tokens:,} tokens",
        data={
            "action": action,
            "confidence": confidence,
            "rationale": rationale[:300],
        },
        elapsed_ms=int(elapsed_s * 1000),
    )
