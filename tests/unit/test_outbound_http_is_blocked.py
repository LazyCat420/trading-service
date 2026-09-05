"""The outbound-HTTP guard must be able to fire, and must not fire on loopback.

A guard that has never blocked anything is indistinguishable from a guard that
cannot block anything. The suite passes network-denied today (6,151 passed, 0
violators on 2026-09-05), so these are the positive controls that keep that
result meaningful.

WHY THE GUARD EXISTS. The tool-execution pre-flight added in `2c59162` probes
the shim with `httpx`. Two cycle-driving tests picked it up transitively and
made live calls to the Gold Spark from the unit suite, aborting both probe
cycles, until `e4cf1da` stubbed them by hand. Hand-stubbing does not scale:
the next module to add an `httpx.post` inherits the same hole, and it fails
SILENTLY whenever the endpoint happens to answer.
"""

from __future__ import annotations

import httpx
import pytest



class TestTheGuardFires:
    def test_a_sync_outbound_call_is_blocked(self):
        with pytest.raises(RuntimeError) as e:
            httpx.Client(timeout=1).get("http://10.0.0.16:5591/vllm-shim/gold-spark/v1/models")
        assert "outbound HTTP blocked" in str(e.value)
        assert "10.0.0.16" in str(e.value), "the message must name the call site"

    @pytest.mark.asyncio
    async def test_an_async_outbound_call_is_blocked(self):
        with pytest.raises(RuntimeError) as e2:
            async with httpx.AsyncClient(timeout=1) as c:
                await c.post("https://example.com/v1/chat/completions", json={})
        assert "outbound HTTP blocked" in str(e2.value)

    def test_the_real_preflight_probe_cannot_reach_the_box(self):
        """The exact call that leaked, driven through the real module.

        `tool_calls_are_parsed` fails OPEN by design, so it returns
        (True, "probe-skipped: ...") rather than raising — the point is that
        the guard stopped the request from leaving the process.
        """
        import asyncio

        from app.services import llm_preflight

        ok, detail = asyncio.run(llm_preflight.tool_calls_are_parsed("dgx_spark"))
        assert ok is True
        assert "probe-skipped" in detail


class TestTheGuardDoesNotOverreach:
    def test_loopback_is_allowed(self):
        """In-process TestClient apps must keep working."""
        # Reaching the transport at all is the proof: the guard would have
        # raised RuntimeError BEFORE any connection was attempted, so any
        # genuine connection failure means loopback was let through.
        with pytest.raises(httpx.TransportError) as e:
            httpx.Client(timeout=0.2).get("http://127.0.0.1:9/nothing-here")
        assert "outbound HTTP blocked" not in str(e.value)

    def test_live_http_opts_out(self, live_http):
        """Requesting the fixture lifts the block, so intent is in the
        signature rather than buried in a patch."""
        with pytest.raises(httpx.TransportError) as e:
            httpx.Client(timeout=0.2).get("http://10.255.255.1/blackhole")
        assert "outbound HTTP blocked" not in str(e.value), (
            "live_http did not lift the block"
        )
