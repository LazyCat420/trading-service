r"""A scraper that answers every call with nothing must be visible somewhere.

The `success: false` branch of `ScraperServiceClient.scrape` deliberately does
NOT call `_note_failure`, with a written reason: the service replied, and a
per-URL miss (a paywall, a thin body) is not an outage. That reasoning is
correct per URL and wrong in aggregate.

Measured 2026-09-13, the state this closes:

  * `self.failures` stays 0 through a 100% miss rate, so `SweepRecord.failed`
    is False and `failure_rate` is 0.0 during a total outage.
  * the per-URL line was demoted to `debug` (it was 60,881 of 72,732 warnings
    in seven days — ~8,700 database rows a day), and `cycle_main.py:442` sets
    the root logger to INFO with `force=True` while nothing anywhere lowers
    this logger, so it now reaches NO SINK AT ALL.
  * `grep -c 'scrape_stats\|stats\['` over the module returned 0 — there was no
    aggregate of any kind.

Net: the scraper could return nothing for every URL, forever, and produce zero
rows, zero log lines and a failure count of zero. The module's own docstring
records an earlier version that "laundered a TOTAL outage" exactly this way.

So: count the miss without calling it a failure, and give the caller one
aggregate to alarm on. One line per sweep, not 8,700 rows a day.
"""

from __future__ import annotations

import pytest

from app.services.scraper_client import ScraperServiceClient


@pytest.fixture
def client():
    c = ScraperServiceClient()
    c.calls = 0
    c.failures = 0
    c.misses = 0
    return c


def _miss(c, n=1):
    """Simulate the `success: false` branch: a call, and a miss, no failure."""
    for _ in range(n):
        c.calls += 1
        c.misses += 1


def _ok(c, n=1):
    for _ in range(n):
        c.calls += 1


def test_a_total_outage_is_reported_even_though_nothing_failed(client):
    sweep = client.sweep()
    _miss(client, 12)

    assert sweep.failures == 0, "a miss must not be counted as a failure"
    assert sweep.failed is False
    assert sweep.misses == 12
    assert sweep.miss_rate == 1.0
    assert sweep.total_miss is True, (
        "every call came back empty and nothing reports it — this is the "
        "'laundered a total outage' shape the module's docstring records"
    )


def test_a_partial_miss_is_not_an_outage(client):
    """The other direction: normal scraping has misses and must stay quiet."""
    sweep = client.sweep()
    _ok(client, 9)
    _miss(client, 3)

    assert sweep.misses == 3
    assert sweep.miss_rate == pytest.approx(0.25)
    assert sweep.total_miss is False, (
        "a paywalled article in an otherwise healthy sweep must not alarm"
    )


def test_an_empty_sweep_is_not_an_outage(client):
    """Zero calls is not a 100% miss rate. Division, and meaning."""
    sweep = client.sweep()
    assert sweep.calls == 0
    assert sweep.miss_rate == 0.0
    assert sweep.total_miss is False


def test_the_sweep_is_a_delta_not_the_process_total(client):
    """A concurrent caller's misses must not land in this sweep."""
    _miss(client, 5)          # another sweep's misses, before ours starts
    sweep = client.sweep()
    _miss(client, 2)

    assert sweep.misses == 2, (
        f"sweep counted {sweep.misses} — it is reading the process-wide total "
        "instead of its own delta"
    )
    assert client.misses == 7


def test_misses_and_failures_stay_distinct(client):
    """Collapsing them would make the outage signal fire on every paywall."""
    sweep = client.sweep()
    _miss(client, 4)
    client.calls += 1
    client._note_failure("http://x", "boom")

    assert sweep.misses == 4
    assert sweep.failures == 1
    assert sweep.failed is True
    assert sweep.total_miss is False, (
        "4 misses + 1 hard failure over 5 calls is not a total miss"
    )


def test_a_timed_out_scrape_counts_as_a_miss(client, monkeypatch):
    """A TIMEOUT is the most common failure here, and it landed nowhere.

    `news_collector` wraps `scrape` in `asyncio.wait_for`. On expiry that
    CANCELS the coroutine, and `asyncio.CancelledError` inherits from
    BaseException — so the `except Exception` handler never ran. `calls` had
    already incremented, so every timeout diluted `miss_rate` DOWNWARD and a
    100% timeout outage read as perfectly healthy:

        calls=1  misses=0  failures=0  miss_rate=0.00  total_miss=False

    An alarm that goes quiet during the outage it was written for is worse than
    no alarm at all.
    """
    import asyncio as _aio

    sweep = client.sweep()

    class _Hang:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            await _aio.sleep(10)

    monkeypatch.setattr(client, "base_url", "http://stub", raising=False)
    monkeypatch.setattr("app.services.scraper_client.httpx.AsyncClient",
                        lambda *a, **k: _Hang())

    async def _drive():
        with pytest.raises(_aio.TimeoutError):
            await _aio.wait_for(client.scrape("http://x"), timeout=0.05)

    _aio.run(_drive())

    assert sweep.calls == 1
    assert sweep.misses == 1, (
        "a timed-out scrape was not counted — CancelledError is a "
        "BaseException and slipped past `except Exception`"
    )
    assert sweep.failures == 0, "a timeout is not a service failure"
    assert sweep.total_miss is True, (
        "every call timed out and total_miss is still False — the alarm is "
        "blind to the failure it exists for"
    )


def test_the_cancellation_is_re_raised_not_swallowed(client, monkeypatch):
    """Counting must not break cancellation. Swallowing it would hang callers."""
    import asyncio as _aio

    class _Hang:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            await _aio.sleep(10)

    monkeypatch.setattr("app.services.scraper_client.httpx.AsyncClient",
                        lambda *a, **k: _Hang())

    async def _drive():
        task = _aio.ensure_future(client.scrape("http://x"))
        await _aio.sleep(0)
        task.cancel()
        with pytest.raises(_aio.CancelledError):
            await task

    _aio.run(_drive())
