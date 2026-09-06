"""One dropped socket cost NBIS six hours of memory consolidation.

MEASURED 2026-09-06, cycle-v3-1788660665, 03:51:27 UTC:

    [ERROR] [PrismAgentCaller] Call failed: Server disconnected without sending a response.
    [ERROR] Failed consolidation for NBIS: Server disconnected without sending a response.

`maybe_consolidate` stamps `_last_attempt[ticker] = now` BEFORE the call and
the cooldown is 6 hours, so a transport failure that the SDK's own classifier
calls TRANSIENT (httpx.RemoteProtocolError -> classify_exception -> TRANSIENT)
was treated exactly like a completed consolidation. Nothing corrupts — the
observations stay unpromoted — but the ticker's canonical memory goes stale
for six hours over a socket, and the janitor deletes promoted rows on a 30-day
clock while unpromoted ones pile up.

Now: a TRANSIENT failure re-opens the gate after a short window; success and
non-transient failures keep the full cooldown. The stamp moves AFTER the
outcome is known.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.memory import consolidator as c

DISCONNECT = httpx.RemoteProtocolError("Server disconnected without sending a response.")
FIVE_OBS = [{"id": i, "observation_text": "x", "created_at": "t"} for i in range(5)]


@pytest.fixture(autouse=True)
def _clean_state():
    c._last_attempt.clear()
    yield
    c._last_attempt.clear()


def _clock(monkeypatch, start=1_000_000.0):
    now = {"t": start}
    monkeypatch.setattr(c.time, "monotonic", lambda: now["t"])
    return now


async def _attempt(fail_with=None):
    call = AsyncMock(side_effect=fail_with) if fail_with else AsyncMock(return_value=("[]", 10, 5))
    with patch.object(c, "get_unpromoted_observations", return_value=FIVE_OBS), \
         patch.object(c, "get_active_canonical_memories", return_value=[]), \
         patch.object(c, "call_prism_agent", call), \
         patch.object(c, "mark_observations_promoted", lambda *a, **k: None), \
         patch.object(c, "log_consolidation_run", lambda *a, **k: None):
        await c.maybe_consolidate("NBIS")
    return call.await_count


@pytest.mark.asyncio
async def test_a_failure_before_the_callee_can_classify_still_closes_the_gate(monkeypatch):
    """Moving the stamp after the outcome opened a second hole.

    `run_ticker_consolidation` classifies only what its OWN inner try catches.
    `get_active_canonical_memories` runs before that try, so a store blip there
    escapes to `maybe_consolidate`'s outer handler — which logged and stamped
    nothing. The cooldown then never closed and the ticker re-attempted on
    every cycle, which is worse than the six hours this fix was written to
    save. Master's pre-call stamp closed the gate whatever happened.
    """
    _clock(monkeypatch)
    reached = {"n": 0}

    def _blip(*a, **k):
        reached["n"] += 1
        raise RuntimeError("mongo blip")

    for _ in range(3):
        with patch.object(c, "get_unpromoted_observations", return_value=FIVE_OBS), \
             patch.object(c, "get_active_canonical_memories", _blip):
            await c.maybe_consolidate("NBIS")

    assert reached["n"] == 1, (
        f"the store was reached {reached['n']}x — the gate never closed, so "
        "every cycle re-attempts a consolidation that just failed"
    )
    assert "NBIS" in c._last_attempt


@pytest.mark.asyncio
async def test_a_dropped_socket_does_not_burn_the_six_hour_cooldown(monkeypatch):
    now = _clock(monkeypatch)
    assert await _attempt(fail_with=DISCONNECT) == 1
    now["t"] += c.TRANSIENT_RETRY_SECONDS + 1
    assert await _attempt() == 1, "after the short window the ticker must be consolidated again"


@pytest.mark.asyncio
async def test_but_it_does_not_hammer_either(monkeypatch):
    now = _clock(monkeypatch)
    await _attempt(fail_with=DISCONNECT)
    now["t"] += c.TRANSIENT_RETRY_SECONDS - 1
    assert await _attempt() == 0


@pytest.mark.asyncio
async def test_a_success_keeps_the_full_cooldown(monkeypatch):
    now = _clock(monkeypatch)
    assert await _attempt() == 1
    now["t"] += c.CONSOLIDATION_COOLDOWN_SECONDS - 1
    assert await _attempt() == 0


@pytest.mark.asyncio
async def test_a_non_transient_failure_keeps_the_full_cooldown(monkeypatch):
    """A model that answers garbage is not a socket; retrying in ten minutes
    would just spend the box again on the same prompt."""
    now = _clock(monkeypatch)
    await _attempt(fail_with=ValueError("bad JSON from the model"))
    now["t"] += c.TRANSIENT_RETRY_SECONDS + 1
    assert await _attempt() == 0


def test_the_short_window_is_short():
    assert 60 <= c.TRANSIENT_RETRY_SECONDS <= 1800 < c.CONSOLIDATION_COOLDOWN_SECONDS
