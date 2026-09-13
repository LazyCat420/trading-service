"""A vendor "no such symbol" is evidence. A DNS failure is not.

`validate_unknown_tickers` calls yfinance once per unknown symbol, with a 20 s
timeout and a 0.3 s sleep between. Its `except` block used to write nothing at
all — not to `company_registry`, not to `FALSE_TICKERS` — so every symbol that
errored was re-looked-up on every future cycle that saw the word.

MEASURED over 30 days: 239 failures, 222 distinct symbols, 93% of them seen
exactly once, and ~7 per cycle against a median of ONE ticker actually
analysed. GABGX (a mutual fund), JUNK (junk bonds), NSSGA (a trade
association), BILLY (a person) and DRCC all reappeared with no registry row.

But 96 of those 239 were `getaddrinfo() thread failed to start` — resource
exhaustion, which says nothing about the symbol. KVHI is a real stock and
failed four times that way. Caching that as a rejection would permanently ban
real tickers, and discovery's whole job is finding names not yet in the
registry. So the split is the fix, and both halves need a test: one that the
junk is remembered, one that the real ticker is NOT.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
import yfinance

from app.processors import ticker_extractor as TE


class _FakeRegistry:
    def __init__(self):
        self.rejected: set[str] = set()
        self.known: set[str] = set()

    def is_known(self, sym):
        return sym in self.known

    def is_rejected(self, sym):
        return sym in self.rejected

    def add_rejected(self, sym):
        self.rejected.add(sym)


@pytest.fixture
def wired(monkeypatch):
    """Route the module's registry, DB write and vendor call into fakes."""
    reg = _FakeRegistry()
    saved: list[str] = []
    monkeypatch.setattr(TE, "get_registry", lambda *a, **k: reg, raising=False)
    monkeypatch.setattr(TE, "_save_rejected_to_db", lambda s: saved.append(s))
    monkeypatch.setattr(TE, "FALSE_TICKERS", set(TE.FALSE_TICKERS))
    # Skip the 0.3 s inter-symbol rate limit. Bind the ORIGINAL first —
    # a stub that calls `asyncio.sleep` calls the stub, not the real one.
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))
    return reg, saved


def _patch_vendor(monkeypatch, fn):
    """Patch `yfinance.Ticker` itself, not a module attribute on the caller.

    `validate_unknown_tickers` does `import yfinance as yf` INSIDE the
    function (ticker_extractor.py:1476), so the name is rebound from
    `sys.modules` on every call and `monkeypatch.setattr(TE, "yf", ...)` is a
    silent no-op — the test passes while the real vendor is called.
    See [[a-function-local-import-defeats-a-module-attribute-patch]].
    """
    monkeypatch.setattr(sys.modules["yfinance"], "Ticker", fn)


def _raise(msg):
    def _boom(*_a, **_k):
        raise RuntimeError(msg)
    return _boom


@pytest.mark.parametrize("msg", [
    "404 Client Error: Not Found for url",
    "No data found for this symbol",
    "possibly delisted; no price data found",
])
def test_a_vendor_rejection_is_remembered(wired, monkeypatch, msg):
    """The repeat-offender bug: this must never be looked up twice."""
    reg, saved = wired
    _patch_vendor(monkeypatch, _raise(msg))

    asyncio.run(TE.validate_unknown_tickers(["GABGX"]))

    assert "GABGX" in reg.rejected, (
        "a vendor rejection was not persisted — it will be re-looked-up on "
        "every future cycle that sees the word"
    )
    assert "GABGX" in saved, "nothing was written to company_registry"
    assert "GABGX" in TE.FALSE_TICKERS


@pytest.mark.parametrize("msg", [
    "curl: (6) getaddrinfo() thread failed to start",
    "can't start new thread",
    "Connection reset by peer",
    "Read timed out",
])
def test_a_transport_failure_is_NOT_remembered(wired, monkeypatch, msg):
    """KVHI is a real stock. Caching DNS exhaustion would ban it forever.

    40% of the measured failures were this class, so getting it wrong is not a
    corner case — it would have been the majority behaviour.
    """
    reg, saved = wired
    _patch_vendor(monkeypatch, _raise(msg))

    asyncio.run(TE.validate_unknown_tickers(["KVHI"]))

    assert "KVHI" not in reg.rejected, (
        f"a transport failure ({msg!r}) was cached as a rejection — this bans "
        "a real ticker permanently"
    )
    assert "KVHI" not in saved
    assert "KVHI" not in TE.FALSE_TICKERS


def test_the_two_classes_are_actually_distinguished(wired, monkeypatch):
    """Non-vacuity control.

    Two implementations that cache everything, or nothing, each pass one of
    the tests above. Only asserting that the SAME call path sorts the two
    differently proves a split exists.
    """
    reg, saved = wired
    _patch_vendor(monkeypatch, _raise("404 Not Found"))
    asyncio.run(TE.validate_unknown_tickers(["JUNKSYM"]))
    _patch_vendor(monkeypatch, _raise("getaddrinfo() thread failed to start"))
    asyncio.run(TE.validate_unknown_tickers(["REALSYM"]))

    assert reg.rejected == {"JUNKSYM"}, (
        f"expected exactly the vendor rejection to be cached, got {reg.rejected}"
    )


def test_an_already_rejected_symbol_never_reaches_the_vendor(wired, monkeypatch):
    """The point of caching: the second lookup must cost nothing."""
    reg, _saved = wired
    reg.add_rejected("GABGX")
    calls = []
    _patch_vendor(monkeypatch, lambda *a, **k: calls.append(a) or _raise("x")())

    out = asyncio.run(TE.validate_unknown_tickers(["GABGX"]))

    assert calls == [], "a cached rejection still paid for a network round trip"
    assert out.get("GABGX") is False
