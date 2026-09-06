"""A failed scrape must never orphan a Chromium.

This is the NAS-fatal one. uvicorn is pid 1 in that container and an app running
as pid 1 never wait()s, so every orphaned browser became a permanent zombie.
20,179 of them exhausted the host's pid space (kernel.pid_max = 32768): fork()
started failing system-wide with EAGAIN, which killed the Docker daemon, cost
systemd its D-Bus name, and took the whole NAS down — twice.
(`trading-service/docs/INCIDENT_2026-08-20_NAS_PID_EXHAUSTION.md`.)

`init: true` and `pids_limit: 1024` now bound the blast radius, but they treat
the symptom: tini reaping orphans MASKS a close() that is still leaking, which
is exactly why this needs a test rather than a watch on the zombie count.

The predecessor of this file lived in `scraper-service/tests/`, which has no
pytest config and is outside trading-service's `testpaths` — so nothing ran it.
It also drove only a `page.goto` failure, and therefore passed while
`p.chromium.launch()` sat OUTSIDE the try/finally it was supposed to be
protected by.
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.scraper.engines import playwright_engine as pe
from app.scraper.engines.playwright_engine import PlaywrightEngine


class _NoRate:
    def acquire(self, domain):
        class _Ctx:
            async def __aenter__(self):
                return None
            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.fixture(autouse=True)
def _reset_browser_semaphore(monkeypatch):
    """The cap is a module global bound to a loop; each test gets a fresh one."""
    monkeypatch.setattr(pe, "_browser_semaphore", None)
    monkeypatch.setattr(pe, "rate_limiter", _NoRate())


def _install_fake_playwright(monkeypatch, *, launch_error=None, failing_step=None):
    """A fake Playwright whose failure point is selectable.

    Returns the mock browser so a test can assert on close().
    """
    mock_browser = AsyncMock()
    mock_browser.close = AsyncMock()

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(return_value="x" * 500)
    mock_page.locator = MagicMock(return_value=MagicMock(count=AsyncMock(return_value=0)))
    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_browser.new_context = AsyncMock(return_value=mock_context)

    if failing_step == "goto":
        mock_page.goto = AsyncMock(side_effect=RuntimeError("navigation failed"))
    if failing_step == "new_context":
        mock_browser.new_context = AsyncMock(side_effect=RuntimeError("context failed"))
    if failing_step == "new_page":
        mock_context.new_page = AsyncMock(side_effect=RuntimeError("page failed"))
    if failing_step == "evaluate":
        mock_page.evaluate = AsyncMock(side_effect=RuntimeError("evaluate failed"))
    if failing_step == "screenshot":
        mock_page.screenshot = AsyncMock(side_effect=RuntimeError("screenshot failed"))

    mock_pw = AsyncMock()
    if launch_error is not None:
        mock_pw.chromium.launch = AsyncMock(side_effect=launch_error)
    else:
        mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)

    class _FakeAsyncPlaywright:
        async def __aenter__(self):
            return mock_pw
        async def __aexit__(self, *a):
            return None

    fake_api = MagicMock()
    fake_api.async_playwright = lambda: _FakeAsyncPlaywright()
    pkg = MagicMock()
    pkg.async_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    monkeypatch.setitem(sys.modules, "playwright_stealth", MagicMock())
    return mock_browser, mock_pw


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_step", ["goto", "new_context", "new_page", "evaluate", "screenshot"])
async def test_the_browser_is_closed_whatever_fails(monkeypatch, failing_step):
    """The original test drove only `goto`. Every one of these is a real way
    for a scrape to die between launch and close."""
    browser, _ = _install_fake_playwright(monkeypatch, failing_step=failing_step)
    opts = {"screenshot": True} if failing_step == "screenshot" else {}
    result = await PlaywrightEngine().fetch("https://example.com/x", opts)

    assert result.success is False
    browser.close.assert_awaited_once(), f"a {failing_step} failure orphaned a Chromium"


@pytest.mark.asyncio
async def test_a_failed_launch_still_exits_the_playwright_context(monkeypatch):
    """What actually reclaims a browser whose launch() failed.

    Worth being precise, because the obvious reading is wrong: moving `launch()`
    inside the try/finally does NOT fix a launch-time leak. If launch() raises
    there is no handle to close in either version — `browser` is simply never
    bound. What reclaims the spawned process is `async with async_playwright()`:
    its __aexit__ stops the driver, and the driver owns the Chromium.

    So the thing to pin is that the context manager is always exited, on the
    failure path as much as the happy one. (The launch-inside-try change is kept
    as defence in depth — it costs nothing and makes the finally total — but it
    is not what makes this safe.)
    """
    exited = []

    class _TrackingPlaywright:
        async def __aenter__(self):
            mock_pw = AsyncMock()
            mock_pw.chromium.launch = AsyncMock(side_effect=RuntimeError("launch timed out"))
            return mock_pw
        async def __aexit__(self, *a):
            exited.append(True)
            return None

    fake_api = MagicMock()
    fake_api.async_playwright = lambda: _TrackingPlaywright()
    pkg = MagicMock(); pkg.async_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    monkeypatch.setitem(sys.modules, "playwright_stealth", MagicMock())

    result = await PlaywrightEngine().fetch("https://example.com/x", {})

    assert result.success is False
    assert "launch timed out" in (result.error or "")
    assert exited == [True], "the driver must be stopped even when launch fails"


@pytest.mark.asyncio
async def test_a_failed_launch_releases_its_browser_slot(monkeypatch):
    """A real deadlock risk, and the reason the cap must sit in a `with`.

    The process-wide cap is 4. If a failing launch kept its slot, four launch
    failures — the exact thing that happens under memory pressure — would wedge
    every future scrape in this worker forever, with /health still green.
    """
    monkeypatch.setattr(pe, "_MAX_CONCURRENT_BROWSERS", 2)
    monkeypatch.setattr(pe, "_browser_semaphore", None)
    _install_fake_playwright(monkeypatch, launch_error=RuntimeError("no memory"))

    engine = PlaywrightEngine()
    for _ in range(5):        # more failures than there are slots
        r = await engine.fetch("https://example.com/x", {})
        assert r.success is False

    # If slots leaked, this would hang rather than return.
    await asyncio.wait_for(engine.fetch("https://example.com/y", {}), timeout=5)


@pytest.mark.asyncio
async def test_launch_is_given_an_explicit_timeout(monkeypatch):
    """A silent 30s default makes a launch stall a mystery rather than an event."""
    _, mock_pw = _install_fake_playwright(monkeypatch)
    await PlaywrightEngine().fetch("https://example.com/x", {})
    assert mock_pw.chromium.launch.await_args.kwargs.get("timeout") == pe._LAUNCH_TIMEOUT_MS


@pytest.mark.asyncio
async def test_a_cancelled_scrape_still_closes_the_browser(monkeypatch):
    """The route now imposes a deadline, so cancellation is a REAL path, not a
    hypothetical. A cancelled coroutine that skips its close() leaks the exact
    browser the finally exists to reclaim — hence asyncio.shield."""
    browser, _ = _install_fake_playwright(monkeypatch)

    started = asyncio.Event()

    async def _hang(*a, **k):
        started.set()
        await asyncio.sleep(3600)

    sys.modules["playwright.async_api"].async_playwright().__aenter__  # touch
    import playwright.async_api as fake  # noqa: F401
    # Make the navigation hang so we can cancel mid-scrape.
    mock_ctx = await browser.new_context()
    (await mock_ctx.new_page()).goto = AsyncMock(side_effect=_hang)

    task = asyncio.create_task(PlaywrightEngine().fetch("https://example.com/x", {}))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    browser.close.assert_awaited_once(), "cancellation must not skip the close"


@pytest.mark.asyncio
async def test_concurrent_browsers_are_capped_per_process(monkeypatch):
    """/scrape/batch built its semaphore PER REQUEST, so it bounded one call and
    nothing bounded the sum. 20 Chromiums is ~2000 tasks against a 1024 pid
    ceiling for the whole container."""
    browser, mock_pw = _install_fake_playwright(monkeypatch)
    monkeypatch.setattr(pe, "_MAX_CONCURRENT_BROWSERS", 2)
    monkeypatch.setattr(pe, "_browser_semaphore", None)

    live = 0
    peak = 0
    real_launch = mock_pw.chromium.launch

    async def _counting_launch(*a, **k):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        return await real_launch(*a, **k)

    async def _counting_close():
        nonlocal live
        live -= 1

    mock_pw.chromium.launch = AsyncMock(side_effect=_counting_launch)
    browser.close = AsyncMock(side_effect=_counting_close)

    engine = PlaywrightEngine()
    await asyncio.gather(*(engine.fetch(f"https://example.com/{i}", {}) for i in range(8)))

    assert peak <= 2, f"{peak} browsers ran at once against a cap of 2"
