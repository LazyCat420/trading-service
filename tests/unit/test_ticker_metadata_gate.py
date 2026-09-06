"""A ticker with no metadata row is invisible to two admission caps.

MEASURED 2026-09-06 (Appendix L.4 of the trading-cycle audit).

`cycle-v3-1788646388` selected ZS, analysed it for 100 minutes and **bought it**
(3.0808 @ $169.84). ZS has **zero rows in `ticker_metadata`** — 1,049 other
tickers have one — and so does SE, which the bot SOLD the same day.

No row means no `sector` and no `market_cap_tier`, and both admission caps read
exactly those fields:

  * the sector cap (`pipeline_service`, max 2 per sector) skips a name whose
    sector is falsy — an unknown sector is *exempt*, not capped;
  * the mega-cap cap tests `market_cap_tier == "mega"`, so an untiered name can
    never be the mega-cap it might actually be.

The `GATEKEEPER_SELECTED` event was already reporting it — `tier_unknown:
['ZS']` — and nothing acted on the report.

This is the same shape as the 2026-08-25 finding that shipped
`scripts/backfill_market_cap_tier.py`: "the enforcement shipped; the data it
reads never did", 510 of 1,049 rows with no tier, AAPL and TSLA among them.
That script is manual. The gate has to fill the gap on the path that consumes
it, or the next untagged name repeats this.

Fails OPEN: a vendor lookup that errors leaves the name exactly as it is today
(`tier_unknown`, admitted), because refusing to analyse a ticker because
yfinance was slow is worse than analysing it uncapped.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.ticker_meta import ensure_ticker_metadata


@pytest.fixture
def store():
    with patch("app.services.ticker_meta.mongo_store") as ms:
        ms.find_docs.return_value = []
        yield ms


def _yf(caps: dict[str, float | None]):
    """A stand-in for yfinance keyed by ticker."""
    def _ticker(sym):
        obj = MagicMock()
        obj.fast_info = {"marketCap": caps.get(sym)}
        obj.info = {"marketCap": caps.get(sym), "sector": "Technology"}
        return obj
    mod = MagicMock()
    mod.Ticker.side_effect = _ticker
    return mod


class TestItFillsTheGap:
    def test_a_ticker_with_no_row_is_upserted_with_its_tier(self, store):
        with patch.dict("sys.modules", {"yfinance": _yf({"ZS": 25e9})}):
            out = ensure_ticker_metadata(["ZS"])

        assert out.get("ZS") == "large"
        assert store.update_docs.called, "nothing was written for a missing row"
        collection, where, update = store.update_docs.call_args[0][:3]
        assert collection == "ticker_metadata"
        assert where == {"ticker": "ZS"}
        assert update["$set"]["market_cap_tier"] == "large"

    def test_the_upsert_flag_is_set_so_a_missing_row_is_created(self, store):
        """`update_docs` without upsert=True matches nothing and writes
        nothing — the exact no-op this function exists to prevent."""
        with patch.dict("sys.modules", {"yfinance": _yf({"ZS": 25e9})}):
            ensure_ticker_metadata(["ZS"])

        assert store.update_docs.call_args.kwargs.get("upsert") is True

    def test_a_mega_cap_is_tiered_mega_so_the_cap_can_see_it(self, store):
        with patch.dict("sys.modules", {"yfinance": _yf({"NVDA": 3.1e12})}):
            assert ensure_ticker_metadata(["NVDA"]).get("NVDA") == "mega"

    def test_it_uses_the_same_thresholds_as_the_loader(self, store):
        """Derived, not transcribed: one authority for the buckets, or the
        gate's `tier == "mega"` test drifts from the writer's."""
        from app.data.sp500_universe import tier_for_market_cap

        caps = {"A": 500e9, "B": 50e9, "C": 5e9, "D": 1e9}
        with patch.dict("sys.modules", {"yfinance": _yf(caps)}):
            out = ensure_ticker_metadata(list(caps))

        for tkr, cap in caps.items():
            assert out.get(tkr) == tier_for_market_cap(cap)


class TestItNeverOverwrites:
    def test_a_ticker_that_already_has_a_tier_is_left_alone(self, store):
        store.find_docs.return_value = [
            {"ticker": "AAPL", "market_cap_tier": "mega", "sector": "Technology"}
        ]
        with patch.dict("sys.modules", {"yfinance": _yf({"AAPL": 1.0})}):
            out = ensure_ticker_metadata(["AAPL"])

        assert out.get("AAPL") == "mega"
        assert not store.update_docs.called, (
            "an existing tier was overwritten — a stale vendor read must never "
            "beat a value already on file"
        )

    def test_only_the_untagged_names_are_looked_up(self, store):
        store.find_docs.return_value = [
            {"ticker": "AAPL", "market_cap_tier": "mega"}
        ]
        yf = _yf({"ZS": 25e9})
        with patch.dict("sys.modules", {"yfinance": yf}):
            ensure_ticker_metadata(["AAPL", "ZS"])

        looked_up = {c.args[0] for c in yf.Ticker.call_args_list}
        assert looked_up == {"ZS"}, f"looked up more than it needed: {looked_up}"


class TestItFailsOpen:
    def test_a_vendor_error_leaves_the_ticker_as_it_is(self, store):
        yf = MagicMock()
        yf.Ticker.side_effect = RuntimeError("yfinance is down")
        with patch.dict("sys.modules", {"yfinance": yf}):
            out = ensure_ticker_metadata(["ZS"])

        assert out.get("ZS") is None
        assert not store.update_docs.called

    def test_a_ticker_with_no_market_cap_is_not_tiered(self, store):
        """ETFs, trusts and dead symbols have no marketCap. Writing a tier for
        them would be inventing one."""
        with patch.dict("sys.modules", {"yfinance": _yf({"SPY": None})}):
            out = ensure_ticker_metadata(["SPY"])

        assert out.get("SPY") is None
        assert not store.update_docs.called

    def test_a_store_failure_does_not_raise_into_the_gatekeeper(self, store):
        store.find_docs.side_effect = RuntimeError("mongo is gone")
        assert ensure_ticker_metadata(["ZS"]) == {}

    def test_yfinance_missing_entirely_is_not_an_error(self, store):
        with patch.dict("sys.modules", {"yfinance": None}):
            assert ensure_ticker_metadata(["ZS"]) == {}

    @pytest.mark.parametrize("junk", [None, [], [""], ["  "], [None]])
    def test_junk_input_is_a_no_op(self, store, junk):
        assert ensure_ticker_metadata(junk) == {}


class TestTheGatekeeperCallsIt:
    def test_the_admission_block_backfills_before_the_caps_run(self):
        """The seam. `_tier_unknown` was computed and only LOGGED; the caps ran
        against the same empty metadata regardless."""
        import inspect

        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service)
        assert "ensure_ticker_metadata" in src, (
            "the gatekeeper still reports tier_unknown without acting on it"
        )

    def test_the_explicit_ticker_path_backfills_too(self):
        """The gate had ONE caller, inside the gatekeeper branch — and MEASURED
        over 21 days, 143 of 194 cycles (74%) never reach it: a Watch Desk trip
        or an operator request prints "discovery & gatekeeper bypassed" and goes
        straight to analysis. So a fund forced onto that path kept a company
        tier, and a name with no row at all was analysed and traded with none:
        ZS (bought 2026-09-06) and SE (bought 08-12, sold 09-05) still have no
        `ticker_metadata` document.
        """
        import inspect

        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service)
        i = src.index("Explicit ticker request honored")
        # the backfill must run BEFORE the path is pinned, not after
        head = src[:i]
        j = head.rindex("if max_tickers:")
        assert "ensure_ticker_metadata" in head[j:], (
            "the explicit-ticker path still skips the metadata gate, so 74% of "
            "cycles trade names the tier gate never saw"
        )
