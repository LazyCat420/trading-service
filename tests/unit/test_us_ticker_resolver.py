"""
Unit tests for US Ticker Resolver.

Tests the core format detection, hard-coded ADR map, and batch resolution logic.
"""

import pytest
from app.utils.us_ticker_resolver import (
    is_us_tradeable,
    resolve_to_us_ticker,
    resolve_tickers_batch,
    _has_foreign_format,
    KNOWN_ADR_MAP,
)


class TestHasForeignFormat:
    """Test the foreign ticker format detector."""

    def test_korean_tickers(self):
        assert _has_foreign_format("000660.KS") is True
        assert _has_foreign_format("005930.KS") is True
        assert _has_foreign_format("035420.KS") is True

    def test_japanese_tickers(self):
        assert _has_foreign_format("6758.T") is True
        assert _has_foreign_format("7203.T") is True

    def test_hong_kong_tickers(self):
        assert _has_foreign_format("9988.HK") is True
        assert _has_foreign_format("0700.HK") is True

    def test_taiwanese_tickers(self):
        assert _has_foreign_format("2330.TW") is True

    def test_european_tickers(self):
        assert _has_foreign_format("ASML.AS") is True
        assert _has_foreign_format("SAP.DE") is True
        assert _has_foreign_format("AZN.L") is True
        assert _has_foreign_format("SHEL.L") is True

    def test_numeric_only_tickers(self):
        assert _has_foreign_format("000660") is True
        assert _has_foreign_format("6758") is True

    def test_us_tickers_not_foreign(self):
        assert _has_foreign_format("AAPL") is False
        assert _has_foreign_format("NVDA") is False
        assert _has_foreign_format("TSLA") is False
        assert _has_foreign_format("SKHYV") is False
        assert _has_foreign_format("TSM") is False

    def test_us_share_classes(self):
        """BRK.B, BF.B are US share class notation, NOT foreign."""
        assert _has_foreign_format("BRK.A") is False
        assert _has_foreign_format("BRK.B") is False
        assert _has_foreign_format("BF.B") is False


class TestIsUsTradeable:
    """Test the US tradeable check."""

    def test_us_tickers_pass(self):
        assert is_us_tradeable("AAPL") is True
        assert is_us_tradeable("NVDA") is True
        assert is_us_tradeable("SKHYV") is True
        assert is_us_tradeable("TSM") is True
        assert is_us_tradeable("BRK.B") is True

    def test_foreign_tickers_fail(self):
        assert is_us_tradeable("000660.KS") is False
        assert is_us_tradeable("6758.T") is False
        assert is_us_tradeable("9988.HK") is False
        assert is_us_tradeable("2330.TW") is False
        assert is_us_tradeable("ASML.AS") is False

    def test_known_adr_map_entries_fail(self):
        """Tickers IN the ADR map are foreign and should fail."""
        assert is_us_tradeable("000660.KS") is False
        assert is_us_tradeable("0700.HK") is False

    def test_empty_ticker(self):
        assert is_us_tradeable("") is False

    def test_whitespace_handling(self):
        assert is_us_tradeable("  AAPL  ") is True
        assert is_us_tradeable("  000660.KS  ") is False


class TestResolveToUsTicker:
    """Test the synchronous ADR resolution (hard-coded map only)."""

    def test_sk_hynix_is_dropped_not_redirected(self):
        """Updated 2026-07-27. This originally asserted 000660.KS -> SKHYV.

        SKHYV is a real SK Hynix ADR with a matching name, but it trades ~1
        bar/month against the KRX line's ~20. In cycle-v3-1785128960 the
        rewrite handed the desk a ticker with 2 price rows, on which a full
        agent panel ran and produced a HOLD @ 54. Resolving to an untradeable
        symbol is worse than not resolving: a dropped ticker is visible in the
        logs, a redirected one is not.

        Verified with scripts/audit_adr_map.py. Re-add only if a liquid,
        name-verified SK Hynix listing appears.
        """
        assert resolve_to_us_ticker("000660.KS") is None

    def test_sony_resolves(self):
        result = resolve_to_us_ticker("6758.T")
        assert result == "SONY"

    def test_tsmc_resolves(self):
        result = resolve_to_us_ticker("2330.TW")
        assert result == "TSM"

    def test_alibaba_resolves(self):
        result = resolve_to_us_ticker("9988.HK")
        assert result == "BABA"

    def test_tencent_resolves(self):
        result = resolve_to_us_ticker("0700.HK")
        assert result == "TCEHY"

    def test_toyota_resolves(self):
        result = resolve_to_us_ticker("7203.T")
        assert result == "TM"

    def test_unknown_foreign_returns_none(self):
        result = resolve_to_us_ticker("UNKNOWN.XX")
        assert result is None

    def test_us_ticker_returns_none(self):
        """US tickers should not be in the map."""
        result = resolve_to_us_ticker("AAPL")
        assert result is None

    def test_case_insensitive(self):
        assert resolve_to_us_ticker("6758.t") == "SONY"


class TestResolveTickersBatch:
    """Test the batch resolution function."""

    def test_mixed_batch(self):
        """Should pass US tickers through, resolve known foreign, drop unknown foreign."""
        input_tickers = ["NVDA", "2330.TW", "AAPL", "6758.T", "999999.KS"]
        result = resolve_tickers_batch(input_tickers)
        assert "NVDA" in result
        assert "AAPL" in result
        assert "TSM" in result    # 2330.TW → TSM
        assert "SONY" in result   # 6758.T → SONY
        assert "999999.KS" not in result  # dropped (foreign format, no known ADR)
        assert "2330.TW" not in result    # resolved, not passed through

    def test_all_us_tickers(self):
        """All US tickers should pass through unchanged."""
        input_tickers = ["AAPL", "NVDA", "TSLA", "MSFT"]
        result = resolve_tickers_batch(input_tickers)
        assert result == ["AAPL", "NVDA", "TSLA", "MSFT"]

    def test_all_foreign_with_known_mapping(self):
        """All foreign with known ADR mappings should resolve."""
        input_tickers = ["2330.TW", "6758.T", "9988.HK"]
        result = resolve_tickers_batch(input_tickers)
        assert result == ["TSM", "SONY", "BABA"]

    def test_empty_list(self):
        result = resolve_tickers_batch([])
        assert result == []

    def test_share_class_not_dropped(self):
        """BRK.B should pass through as US share class."""
        result = resolve_tickers_batch(["BRK.B", "BF.B"])
        assert "BRK.B" in result
        assert "BF.B" in result


class TestKnownADRMap:
    """Validate the integrity of the hard-coded ADR map."""

    def test_map_carries_the_liquid_cross_listings(self):
        """Updated 2026-07-27 — was `test_map_has_sk_hynix`.

        The map's job is not "contain every foreign ticker" but "redirect only
        where the destination is the same company AND actually trades". Four
        entries failed that on live data and were removed; these are the
        anchors that pass. See scripts/audit_adr_map.py.
        """
        for foreign, us in (("6758.T", "SONY"), ("2330.TW", "TSM"),
                            ("9988.HK", "BABA"), ("ASML.AS", "ASML")):
            assert KNOWN_ADR_MAP.get(foreign) == us

    def test_audited_bad_destinations_stay_out(self):
        """Guards against a well-meaning re-add of a dead or mismatched ADR."""
        for dead in ("SKHYV", "NPSNY", "MRAAY", "KRMAY", "KYOEY"):
            assert dead not in KNOWN_ADR_MAP.values()

    def test_us_values_have_no_dots(self):
        """All US ticker values should be plain alphanumeric (no dots)."""
        for foreign, us in KNOWN_ADR_MAP.items():
            assert "." not in us, f"US ticker {us} (from {foreign}) contains a dot"
            assert us.isalpha(), f"US ticker {us} (from {foreign}) is not all letters"

    def test_map_is_nonempty(self):
        assert len(KNOWN_ADR_MAP) >= 20, "Expected at least 20 known ADR mappings"
