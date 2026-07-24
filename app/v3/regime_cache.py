"""Per-cycle cache for the Market Regime classification.

The Regime Engine classifies the GLOBAL market state — its own prompt says
"never individual tickers". But ``run_v3_pipeline`` is invoked once per ticker,
so the engine ran once per ticker too, asking the same question about the same
market up to six times concurrently and getting different answers:

    over 14 days, 35 of 64 multi-ticker cycles disagreed with THEMSELVES —
    the same cycle classified one ticker DEEP_DISCOUNT and another
    CONTRADICTORY, minutes apart, off the same macro snapshot.

That is not a prompt problem. The regime label picks which Board persona makes
the final call (Buffett vs Jane Street vs Simons), so per-ticker drift meant
the persona routing was partly noise. 25 of 121 cycles also reported more than
one VIX level, including one cycle citing 15.03, 15.57 and 22.00 at once.

One classification per cycle, shared by every ticker in it. The lock is held
across the LLM call on purpose: tickers 2..N wait for the first result instead
of racing to compute their own. Wall clock is unchanged for a single wave (they
used to spend that time computing in parallel anyway) and strictly better for
watchlists larger than the concurrency cap, where later waves find it warm.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# cycle_id -> regime_classification artifact
_CACHE: dict[str, dict] = {}
# cycle_id -> lock serializing the compute-or-reuse decision
_LOCKS: dict[str, asyncio.Lock] = {}
# Insertion order for eviction; a long-lived process runs many cycles.
_ORDER: list[str] = []
_MAX_CYCLES = 8


def get_lock(cycle_id: str) -> asyncio.Lock:
    """The per-cycle lock. Safe to call from sync code: dict.setdefault is
    atomic with respect to the event loop (no await between check and set)."""
    return _LOCKS.setdefault(cycle_id, asyncio.Lock())


def get(cycle_id: str) -> dict | None:
    """The regime already classified for this cycle, if any."""
    artifact = _CACHE.get(cycle_id)
    return dict(artifact) if isinstance(artifact, dict) else None


def put(cycle_id: str, artifact: dict) -> None:
    """Store this cycle's classification and evict the oldest cycles."""
    if not cycle_id or not isinstance(artifact, dict) or not artifact:
        return
    if cycle_id not in _CACHE:
        _ORDER.append(cycle_id)
    _CACHE[cycle_id] = dict(artifact)

    while len(_ORDER) > _MAX_CYCLES:
        stale = _ORDER.pop(0)
        _CACHE.pop(stale, None)
        _LOCKS.pop(stale, None)


def clear(cycle_id: str = "") -> None:
    """Drop one cycle (or everything). Used by tests."""
    if cycle_id:
        _CACHE.pop(cycle_id, None)
        _LOCKS.pop(cycle_id, None)
        if cycle_id in _ORDER:
            _ORDER.remove(cycle_id)
        return
    _CACHE.clear()
    _LOCKS.clear()
    _ORDER.clear()
