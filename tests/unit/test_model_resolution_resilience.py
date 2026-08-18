"""A five-minute-cached probe must not fail a call it already knows the answer to.

`resolve_default_model_for_agent` calls `get_live_model_from_vllm` for EVERY
agent, so whatever this function raises, that agent's run raises too. On
2026-08-06 the first real gatekeeper shadow died on:

    VLLM endpoint offline: http://10.0.0.30:8000 (error: )

An httpx timeout stringifies to the empty string, which is the whole of that
`(error: )` — the message named a cause ("offline") it had not established and
gave nothing to diagnose with. The box was not offline: it answered a direct
probe 37ms later, and measured afterwards it never exceeded 70ms — 0/30 probes
over the old 2s budget, idle AND under 8 concurrent generations.

So the mechanism remains unproven, and these tests do not assert one. They
assert the property that makes the mechanism not matter: a transient probe
failure costs a refresh, not a call, for as long as a recent answer is in hand.
"""

import time
from unittest.mock import patch

import httpx
import pytest

from app.services import prism_agent_caller as pac

URL = "http://10.0.0.30:8000"
MODEL = "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"


class _Resp:
    def __init__(self, status=200, models=(MODEL,)):
        self.status_code = status
        self._models = models

    def json(self):
        return {"data": [{"id": m} for m in self._models]}


def _client(side_effect=None, response=None):
    """Patch httpx.AsyncClient so no request leaves the process."""
    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, _url):
            if side_effect is not None:
                raise side_effect
            return response

    return patch("httpx.AsyncClient", _C)


@pytest.fixture(autouse=True)
def clean_cache():
    pac._dynamic_model_cache.pop(URL, None)
    yield
    pac._dynamic_model_cache.pop(URL, None)


class TestTheHappyPath:
    @pytest.mark.asyncio
    async def test_it_returns_and_caches_the_model(self):
        with _client(response=_Resp()):
            assert await pac.get_live_model_from_vllm(URL) == MODEL

        assert pac._dynamic_model_cache[URL][0] == MODEL

    @pytest.mark.asyncio
    async def test_a_fresh_cache_short_circuits_the_probe(self):
        pac._dynamic_model_cache[URL] = (MODEL, time.time())

        # Any request at all would raise here.
        with _client(side_effect=AssertionError("must not probe")):
            assert await pac.get_live_model_from_vllm(URL) == MODEL


class TestATransientFailureCostsARefreshNotACall:
    """The property the outage argues for."""

    @pytest.mark.asyncio
    async def test_a_timeout_falls_back_to_the_cached_id(self):
        pac._dynamic_model_cache[URL] = (MODEL, time.time() - 600)  # stale, in grace

        with _client(side_effect=httpx.ReadTimeout("")):
            assert await pac.get_live_model_from_vllm(URL) == MODEL

    @pytest.mark.asyncio
    async def test_an_empty_cache_still_raises(self):
        """Degrading is for a KNOWN answer. With nothing cached there is
        nothing to be confident about, and inventing a model id would send the
        call to whatever prism defaults to."""
        with _client(side_effect=httpx.ReadTimeout("")):
            with pytest.raises(RuntimeError) as e:
                await pac.get_live_model_from_vllm(URL)

        assert "ReadTimeout" in str(e.value)

    @pytest.mark.asyncio
    async def test_a_cache_older_than_the_grace_window_raises(self):
        """An hour bounds how long we can be wrong about a reloaded box."""
        pac._dynamic_model_cache[URL] = (MODEL, time.time() - pac._STALE_MODEL_GRACE_S - 60)

        with _client(side_effect=httpx.ReadTimeout("")):
            with pytest.raises(RuntimeError):
                await pac.get_live_model_from_vllm(URL)

    @pytest.mark.asyncio
    async def test_force_refresh_still_degrades_rather_than_failing(self):
        """force_refresh means "re-check", not "fail if you cannot"."""
        pac._dynamic_model_cache[URL] = (MODEL, time.time() - 10)

        with _client(side_effect=httpx.ConnectTimeout("")):
            assert await pac.get_live_model_from_vllm(URL, force_refresh=True) == MODEL


class TestTheErrorSaysWhatWentWrong:
    @pytest.mark.asyncio
    async def test_the_exception_type_is_in_the_message(self):
        """`(error: )` is what an httpx timeout looks like when only its
        str() is used, and it cost a diagnostic step."""
        with _client(side_effect=httpx.ConnectTimeout("")):
            with pytest.raises(RuntimeError) as e:
                await pac.get_live_model_from_vllm(URL)

        msg = str(e.value)
        assert "ConnectTimeout" in msg
        assert "(error: )" not in msg
        assert "<no message>" in msg

    @pytest.mark.asyncio
    async def test_a_non_200_is_reported_as_such(self):
        with _client(response=_Resp(status=503, models=())):
            with pytest.raises(RuntimeError) as e:
                await pac.get_live_model_from_vllm(URL)

        assert "503" in str(e.value)

    @pytest.mark.asyncio
    async def test_an_empty_model_list_is_not_a_success(self):
        with _client(response=_Resp(models=())):
            with pytest.raises(RuntimeError):
                await pac.get_live_model_from_vllm(URL)


class TestItRetriesBeforeGivingUp:
    @pytest.mark.asyncio
    async def test_the_probe_is_attempted_twice(self):
        attempts = []

        class _C:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, _url):
                attempts.append(1)
                raise httpx.ReadTimeout("")

        with patch("httpx.AsyncClient", _C):
            with pytest.raises(RuntimeError):
                await pac.get_live_model_from_vllm(URL)

        assert len(attempts) == 2

    @pytest.mark.asyncio
    async def test_a_second_attempt_that_succeeds_is_used(self):
        state = {"n": 0}

        class _C:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, _url):
                state["n"] += 1
                if state["n"] == 1:
                    raise httpx.ReadTimeout("")
                return _Resp()

        with patch("httpx.AsyncClient", _C):
            assert await pac.get_live_model_from_vllm(URL) == MODEL
