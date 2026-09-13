"""A transient blip must not become a permanent, present-tense accusation.

The container logs, ~2.4 times a day:

    [telemetry] trading-client unreachable at [...] — system log events are
    being dropped (set TRADING_CLIENT_URL to fix)

All three clauses were wrong at the moment it was printed. Probed live: the
endpoint answers 200 in 13-37 ms, `TRADING_CLIENT_URL` was already set
correctly, and the client's history buffer held 134 delivered events spanning
the window — with delivery resuming 20 seconds after one such warning.

The cause is `_warned_unreachable`, a module-global one-shot that is set and
never reset. One request exceeding the 3.0 s timeout latches it for the life of
the process, and the message is written in the present continuous, so an
operator reads a recovered blip as an ongoing outage and is sent to fix a
variable that was never wrong.

Two more defects in the same 77 lines:
  * `_CANDIDATE_BASES` listed `10.0.0.16:8888` TWICE (the env var is set to the
    same host that is hardcoded), so a real outage paid the 3.0 s timeout three
    times against one dead address.
  * `loop.create_task(_send())` discarded the handle. The loop may drop that
    task at any GC point — an event lost with NO warning at all, which is worse
    than the failure this module exists to report.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.telemetry import system_telemetry as ST


@pytest.fixture(autouse=True)
def _reset():
    ST._working_base = None
    ST._dropped_since_ok = 0
    ST._dropped_total = 0
    ST._inflight.clear()
    yield
    ST._working_base = None
    ST._dropped_since_ok = 0
    ST._dropped_total = 0
    ST._inflight.clear()


class _Resp:
    def __init__(self, code):
        self.status_code = code


class _Client:
    """Stands in for httpx.AsyncClient; `codes` is consumed one per POST."""

    def __init__(self, codes, *a, **k):
        self._codes = codes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        code = self._codes.pop(0)
        if isinstance(code, Exception):
            raise code
        return _Resp(code)


def _wire(monkeypatch, codes):
    monkeypatch.setattr(ST.httpx, "AsyncClient",
                        lambda *a, **k: _Client(codes))


def test_the_candidate_bases_hold_no_duplicate(monkeypatch):
    """A dead host must cost one timeout, not two."""
    monkeypatch.setenv("TRADING_CLIENT_URL", "http://10.0.0.16:8888")
    bases = ST._bases()
    assert len(bases) == len(set(bases)), f"duplicate base in {bases}"
    assert bases[0] == "http://10.0.0.16:8888", "the env override must stay first"


def test_the_env_override_is_still_honoured(monkeypatch):
    monkeypatch.setenv("TRADING_CLIENT_URL", "http://example:9/")
    assert ST._bases()[0] == "http://example:9"


def test_a_recovery_is_reported_and_the_warning_does_not_latch(monkeypatch, caplog):
    """The regression itself: drop, then succeed, and say so."""
    _wire(monkeypatch, [RuntimeError("down")] * len(ST._CANDIDATE_BASES) + [200])
    with caplog.at_level(logging.INFO):
        ST.send_system_log("AGENT", "one")

    # first pass dropped
    assert ST.telemetry_drop_stats()["dropped_since_ok"] == 1
    # second pass succeeds and RESETS
    ST.send_system_log("AGENT", "two")
    stats = ST.telemetry_drop_stats()
    assert stats["dropped_since_ok"] == 0, (
        "a successful delivery did not clear the drop counter — the module is "
        "still latching, and the next warning will describe a past outage in "
        "the present tense"
    )
    assert stats["dropped_total"] == 1, "the total must survive the reset"
    assert any("reachable again" in r.message for r in caplog.records), (
        "recovery was not reported; an operator sees only the warning"
    )


def test_the_warning_fires_once_per_outage_not_once_per_process(monkeypatch, caplog):
    """Outage, recovery, outage — the second outage must warn again."""
    n = len(ST._CANDIDATE_BASES)
    _wire(monkeypatch, [RuntimeError("down")] * n)
    with caplog.at_level(logging.WARNING):
        ST.send_system_log("AGENT", "a")
        _wire(monkeypatch, [200])
        ST.send_system_log("AGENT", "b")
        _wire(monkeypatch, [RuntimeError("down")] * n)
        ST.send_system_log("AGENT", "c")
    warns = [r for r in caplog.records
             if r.levelno >= logging.WARNING and "unreachable" in r.message]
    assert len(warns) == 2, (
        f"expected one warning per outage, got {len(warns)} — a one-shot flag "
        "reports the first outage and silences every later one"
    )


def test_the_message_does_not_blame_the_env_var(monkeypatch, caplog):
    """It was set correctly every time this fired. Do not send anyone there."""
    _wire(monkeypatch, [RuntimeError("down")] * len(ST._CANDIDATE_BASES))
    with caplog.at_level(logging.WARNING):
        ST.send_system_log("AGENT", "x")
    text = " ".join(r.message for r in caplog.records)
    assert "set TRADING_CLIENT_URL to fix" not in text
    assert "are being dropped" not in text, (
        "present-continuous claim about an event that may already have recovered"
    )


def test_an_inflight_task_is_strongly_referenced():
    """A discarded task handle is an event lost with no warning at all."""
    async def _drive():
        ST.send_system_log("AGENT", "held")
        assert ST._inflight, (
            "create_task's handle was discarded — the loop may collect this "
            "task mid-flight and the event vanishes silently"
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())
    assert not ST._inflight, "the done-callback did not discard the finished task"
