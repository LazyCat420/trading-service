"""A silent ticker rewrite must land on the same, tradeable company.

`KNOWN_ADR_MAP` rewrites a requested ticker before anything else runs. If an
entry is wrong the whole cycle — collection, analysis, decision, trade —
happens against a different security and nothing says so.

Audited against live market data 2026-07-27, four of 34 entries were broken:

    000660.KS -> SKHYV   real ADR, correct name, but 1 bar/month against the
                         KRX line's 20. The cycle produced a full agent panel
                         and a HOLD @ 54 off 2 price rows.
    035420.KS -> NPSNY   NAVER (Korea) -> Naspers (South Africa). Wrong company.
    2454.TW   -> MRAAY   MediaTek (Taiwan) -> Murata (Japan). Wrong company.
    035720.KS -> KRMAY   Kakao -> dead symbol, 0 bars, 404 on quote lookup.
    6861.T    -> KYOEY   Keyence -> dead symbol. Replaced with KYCCF.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.utils import us_ticker_resolver as r


class TestBrokenMappingsAreGone:
    @pytest.mark.parametrize("dest", ["SKHYV", "NPSNY", "MRAAY", "KRMAY", "KYOEY"])
    def test_a_known_bad_destination_is_not_reachable(self, dest):
        assert dest not in r.KNOWN_ADR_MAP.values(), (
            f"{dest} was removed after the 2026-07-27 audit — re-adding it "
            "silently redirects a cycle to a dead or different security"
        )

    def test_removing_an_entry_drops_the_ticker_rather_than_guessing(self):
        """A missing mapping is safe: resolve returns None, the batch resolver
        logs and drops. A WRONG mapping is not — it trades something else."""
        for foreign in ("000660.KS", "035420.KS", "2454.TW", "035720.KS"):
            assert r.resolve_to_us_ticker(foreign) is None

    def test_keyence_points_at_the_live_symbol(self):
        assert r.KNOWN_ADR_MAP.get("6861.T") == "KYCCF"

    def test_surviving_mappings_are_still_wired(self):
        """The removals must not have cost the good entries."""
        for foreign, us in (("6758.T", "SONY"), ("2330.TW", "TSM"),
                            ("9988.HK", "BABA"), ("ASML.AS", "ASML")):
            assert r.resolve_to_us_ticker(foreign) == us

    def test_a_dropped_ticker_is_reported_not_silently_swallowed(self):
        with patch.object(r.logger, "warning") as warn:
            out = r.resolve_tickers_batch(["000660.KS", "AAPL"])
        assert out == ["AAPL"]
        assert warn.called, "dropping a requested ticker must be logged"


class TestIdentityMatcherDoesNotBreakGoodEntries:
    """The audit script's matcher is itself a hazard.

    Reading only `shortName` flagged 9888.HK->BIDU ("BIDU-SW"),
    9999.HK->NTES and 9618.HK->JD ("JD-SW") as WRONG COMPANY. Acting on that
    would have deleted three correct mappings. HK cross-listings publish a
    trading-suffix shortName and the real name in longName.
    """

    def test_hk_suffix_names_still_match(self):
        from scripts.audit_adr_map import identity_matches

        assert identity_matches(["BIDU-SW", "BAIDU, INC."],
                                ["BAIDU, INC."]) is True
        assert identity_matches(["JD-SW", "JD.COM, INC."],
                                ["JD.COM, INC."]) is True

    def test_a_genuinely_different_company_is_caught(self):
        from scripts.audit_adr_map import identity_matches

        assert identity_matches(["NAVER"], ["NASPERS LTD."]) is False
        assert identity_matches(["MEDIATEK INC"],
                                ["MURATA MANUFACTURING INC."]) is False

    def test_noise_words_alone_never_count_as_a_match(self):
        """'GROUP HOLDING LIMITED' matches half the market."""
        from scripts.audit_adr_map import identity_matches

        assert identity_matches(["ACME GROUP HOLDINGS LIMITED"],
                                ["ZENITH GROUP HOLDINGS LIMITED"]) is False

    def test_missing_names_are_unverifiable_not_a_failure(self):
        """A thin or delisted symbol may publish no name. That is a prompt to
        look, not grounds to delete a mapping."""
        from scripts.audit_adr_map import identity_matches

        assert identity_matches([], ["SONY GROUP"]) is None
        assert identity_matches(["SONY GROUP"], []) is None


def _real_has_price_history():
    """The unstubbed function.

    conftest installs an autouse stub making `has_price_history` always-True,
    so the rest of the suite exercises policy gates rather than fixture
    tickers' missing data. These tests are ABOUT that function, so they pull
    the original out of the module's source rather than reading the patched
    attribute.
    """
    import importlib

    import app.quant.technical_baseline as tb

    return importlib.reload(tb).has_price_history


class TestMinimumTradeableHistory:
    """Two price rows is not thin data — it is no computable technicals."""

    def _probe(self, rows_available: int) -> bool:
        """Run the probe against a `price_history` holding `rows_available` bars.

        `has_price_history` imports `mongo_store` INSIDE the function, so
        patching the module attribute is a silent no-op — the patch has to land
        on `app.db.mongo_store.count_docs` itself.
        """
        probe = _real_has_price_history()
        with patch("app.db.mongo_store.count_docs",
                   return_value=rows_available) as count:
            out = probe("X")
        # The bar count must come from the ticker's OWN rows: a probe counting
        # the whole collection passes for every ticker on earth.
        count.assert_called_once_with("price_history", {"ticker": "X"})
        return out

    def test_the_skhyv_case_is_rejected(self):
        assert self._probe(2) is False

    def test_zero_rows_is_still_rejected(self):
        assert self._probe(0) is False

    def test_a_ticker_with_enough_history_passes(self):
        from app.quant.technical_baseline import MIN_TRADEABLE_BARS

        assert self._probe(MIN_TRADEABLE_BARS) is True
        assert self._probe(500) is True

    def test_the_threshold_matches_the_shortest_indicator_window(self):
        """RSI-14 and ATR-14 are the shortest windows the desk quotes. Below
        that, every level in the artifact would be invented."""
        from app.quant.technical_baseline import MIN_TRADEABLE_BARS

        assert MIN_TRADEABLE_BARS == 14

    def test_the_threshold_does_not_exclude_recent_listings(self):
        """A 3-month-old listing is real and tradeable. Setting this to
        SMA-200's window would blackball every recent IPO — the graded
        freshness block already reports which indicators are missing."""
        from app.quant.technical_baseline import MIN_TRADEABLE_BARS

        assert MIN_TRADEABLE_BARS < 60
        assert self._probe(63) is True

    def test_an_empty_ticker_is_rejected_without_a_query(self):
        probe = _real_has_price_history()
        with patch("app.db.mongo_store.count_docs",
                   side_effect=AssertionError("must not query")):
            assert probe("") is False
