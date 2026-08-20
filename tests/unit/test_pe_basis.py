"""The P/E basis resolver, pinned to the live rows that produced it.

Every number below was read out of production Mongo on 2026-08-20, not made
up: the COF quarterly EPS series, the two vendor P/Es, and the five tickers
whose disagreement flips which vendor is right.
"""
import pytest
from datetime import datetime
from unittest.mock import patch

from app.quant import pe_basis


# COF quarterlies as stored in `financial_history`, newest first.
COF_QUARTERS = [
    {"period_end": datetime(2026, 6, 30), "eps": 4.73},
    {"period_end": datetime(2026, 3, 31), "eps": 3.34},
    {"period_end": datetime(2025, 12, 31), "eps": 3.26},
    {"period_end": datetime(2025, 9, 30), "eps": 4.83},
]
# The window one quarter older — the one finviz was still summing.
COF_QUARTERS_WITH_CHARGE = [
    {"period_end": datetime(2026, 3, 31), "eps": 3.34},
    {"period_end": datetime(2025, 12, 31), "eps": 3.26},
    {"period_end": datetime(2025, 9, 30), "eps": 4.83},
    {"period_end": datetime(2025, 6, 30), "eps": -8.58},  # Discover charge
]


def _periods(quarters):
    return patch("app.quant.valuation_block._fetch_periods",
                 return_value=list(quarters))


class TestTTMEps:
    def test_the_two_windows_that_produced_the_whole_finding(self):
        """Same company, same day, two windows, a 5.7x difference in P/E."""
        with _periods(COF_QUARTERS):
            current = pe_basis.ttm_eps("COF")
        with _periods(COF_QUARTERS_WITH_CHARGE):
            stale = pe_basis.ttm_eps("COF")

        assert round(current["eps_ttm"], 2) == 16.16
        assert round(stale["eps_ttm"], 2) == 2.85

        price = 227.0
        assert round(pe_basis.implied_pe(price, current["eps_ttm"])) == 14
        assert round(pe_basis.implied_pe(price, stale["eps_ttm"])) == 80
        # Neither vendor was wrong. That is the point — the window was missing.

    def test_a_partial_year_is_refused_not_summed(self):
        """3-of-4 is a 25% understatement wearing the costume of a real number."""
        with _periods(COF_QUARTERS[:3]):
            assert pe_basis.ttm_eps("COF")["eps_ttm"] is None

    def test_one_missing_eps_refuses_the_whole_sum(self):
        holed = [dict(q) for q in COF_QUARTERS]
        holed[2]["eps"] = None
        with _periods(holed):
            assert pe_basis.ttm_eps("COF")["eps_ttm"] is None

    def test_a_window_that_is_not_twelve_months_is_flagged(self):
        """Restated/shifted fiscal boundaries — the SMCI shape."""
        shifted = [
            {"period_end": datetime(2026, 6, 30), "eps": 1.0},
            {"period_end": datetime(2026, 3, 31), "eps": 1.0},
            {"period_end": datetime(2025, 12, 31), "eps": 1.0},
            {"period_end": datetime(2023, 9, 30), "eps": 1.0},  # 2 years back
        ]
        with _periods(shifted):
            out = pe_basis.ttm_eps("SMCI")
        assert out["span_ok"] is False
        assert out["eps_ttm"] == 4.0  # summed, but the window is marked bad

    def test_a_bad_window_is_not_used_as_a_reference(self):
        """span_ok False must not silently adjudicate anything."""
        shifted = [
            {"period_end": datetime(2026, 6, 30), "eps": 1.0},
            {"period_end": datetime(2026, 3, 31), "eps": 1.0},
            {"period_end": datetime(2025, 12, 31), "eps": 1.0},
            {"period_end": datetime(2023, 9, 30), "eps": 1.0},
        ]
        with _periods(shifted):
            out = pe_basis.resolve_pe(
                "SMCI", price=100.0,
                rows=[{"source": "yfinance", "pe_ratio": 25.0,
                       "snapshot_date": datetime(2026, 8, 20)}])
        assert out["basis"] != pe_basis.BASIS_TTM_EPS


class TestAdjudication:
    @pytest.mark.parametrize("ticker,yf,finviz,implied,winner", [
        # Measured 2026-08-20. The winner is NOT the same vendor each time —
        # this table is the reason there is no vendor precedence in the module.
        ("COF", 12.195, 77.19, 13.8, "yfinance"),
        ("DD", 59.438, 347.41, 61.6, "yfinance"),
        ("NRG", 30.145, 149.96, 32.4, "yfinance"),
        ("AEP", 108.276, 19.71, 21.8, "finviz"),
        ("PSA", 155.947, 30.92, 32.7, "finviz"),
    ])
    def test_the_reference_picks_the_side_a_precedence_rule_would_miss(
            self, ticker, yf, finviz, implied, winner):
        out = pe_basis.adjudicate(
            [{"source": "yfinance", "pe_ratio": yf,
              "snapshot_date": datetime(2026, 8, 20)},
             {"source": "finviz", "pe_ratio": finviz,
              "snapshot_date": datetime(2026, 8, 11)}],
            reference=implied,
        )
        assert out["source"] == winner, (
            f"{ticker}: implied {implied} should pick {winner}")
        assert out["basis"] == pe_basis.BASIS_TTM_EPS

    def test_a_fixed_vendor_precedence_would_be_wrong_on_two_of_five(self):
        """The negative control for the design decision itself."""
        cases = [("COF", 12.195, 77.19, 13.8), ("DD", 59.438, 347.41, 61.6),
                 ("NRG", 30.145, 149.96, 32.4), ("AEP", 108.276, 19.71, 21.8),
                 ("PSA", 155.947, 30.92, 32.7)]
        always_yf_wrong = sum(
            1 for _, yf, fv, imp in cases
            if pe_basis.adjudicate(
                [{"source": "yfinance", "pe_ratio": yf, "snapshot_date": None},
                 {"source": "finviz", "pe_ratio": fv, "snapshot_date": None}],
                imp)["source"] != "yfinance")
        assert always_yf_wrong == 2

    def test_the_loser_is_recorded_not_dropped(self):
        out = pe_basis.adjudicate(
            [{"source": "yfinance", "pe_ratio": 12.195, "snapshot_date": None},
             {"source": "finviz", "pe_ratio": 77.19, "snapshot_date": None}],
            reference=13.8)
        assert [r["source"] for r in out["rejected"]] == ["finviz"]
        assert out["disagreement"] == pytest.approx(6.33, abs=0.01)

    def test_agreement_raises_no_disagreement_flag(self):
        out = pe_basis.adjudicate(
            [{"source": "yfinance", "pe_ratio": 12.1, "snapshot_date": None},
             {"source": "finviz", "pe_ratio": 12.9, "snapshot_date": None}],
            reference=12.5)
        assert out["disagreement"] is None

    def test_no_reference_falls_back_to_newest_and_SAYS_SO(self):
        """The status quo is allowed — but it must be labelled, so 'checked and
        agreed' is distinguishable from 'nobody checked'."""
        out = pe_basis.adjudicate(
            [{"source": "finviz", "pe_ratio": 77.19,
              "snapshot_date": datetime(2026, 8, 11)},
             {"source": "yfinance", "pe_ratio": 12.195,
              "snapshot_date": datetime(2026, 8, 20)}],
            reference=None)
        assert out["source"] == "yfinance"       # newest
        assert out["basis"] == pe_basis.BASIS_NONE


class TestDirtyValues:
    def test_infinity_never_wins(self):
        """SDA stores pe_ratio = inf. inf beats every threshold silently."""
        out = pe_basis.adjudicate(
            [{"source": "yfinance", "pe_ratio": float("inf"),
              "snapshot_date": datetime(2026, 8, 20)},
             {"source": "finviz", "pe_ratio": 413.78,
              "snapshot_date": datetime(2026, 8, 11)}],
            reference=400.0)
        assert out["source"] == "finviz"
        assert out["value"] == 413.78

    def test_a_string_pe_is_not_a_pe(self):
        """One yfinance row stores pe_ratio as a str."""
        assert pe_basis._num("12.2") is None
        assert pe_basis._num(True) is None
        assert pe_basis._num(float("nan")) is None

    def test_negative_eps_yields_no_pe_rather_than_a_negative_one(self):
        assert pe_basis.implied_pe(227.0, -8.58) is None
        assert pe_basis.implied_pe(227.0, 0.0) is None


class TestDescribe:
    def test_it_states_the_window(self):
        with _periods(COF_QUARTERS):
            out = pe_basis.resolve_pe(
                "COF", price=227.0,
                rows=[{"source": "yfinance", "pe_ratio": 12.195,
                       "snapshot_date": datetime(2026, 8, 20),
                       "market_cap": 135417856000, "net_income": 9822999552},
                      {"source": "finviz", "pe_ratio": 77.19,
                       "snapshot_date": datetime(2026, 8, 11),
                       "market_cap": 134330000000.0, "net_income": 2880000000.0}])
        line = pe_basis.describe(out)
        assert "P/E 12.2" in line
        assert "TTM EPS 16.16 through 2026-06-30" in line
        assert "disagree 6.33x" in line
        assert "finviz 77.2" in line

    def test_an_unverified_basis_says_unknown_instead_of_implying_one(self):
        with _periods([]):
            out = pe_basis.resolve_pe(
                "XYZ", price=None,
                rows=[{"source": "yfinance", "pe_ratio": 20.0,
                       "snapshot_date": datetime(2026, 8, 20)}])
        assert "TTM window UNKNOWN" in pe_basis.describe(out)


class TestTheBasisLabelIsHonest:
    """The module exists to stop a number travelling without its basis. The
    first draft still labelled the mcap/net_income fallback as
    `ttm_eps_from_quarterlies` — the exact defect, in the provenance code."""

    def test_the_fallback_is_not_labelled_as_a_ttm_eps_adjudication(self):
        with _periods([]):  # no quarterlies at all
            out = pe_basis.resolve_pe(
                "DD", price=None,
                rows=[{"source": "yfinance", "pe_ratio": 59.438,
                       "snapshot_date": datetime(2026, 8, 20),
                       "market_cap": 61_600_000_000, "net_income": 1_000_000_000},
                      {"source": "finviz", "pe_ratio": 347.41,
                       "snapshot_date": datetime(2026, 8, 15)}])
        assert out["source"] == "yfinance"          # still adjudicated
        assert out["basis"] == pe_basis.BASIS_MCAP_NI   # but says HOW
        assert out["eps_ttm"] is None

    def test_a_real_ttm_adjudication_is_labelled_as_one(self):
        with _periods(COF_QUARTERS):
            out = pe_basis.resolve_pe(
                "COF", price=227.0,
                rows=[{"source": "yfinance", "pe_ratio": 12.195,
                       "snapshot_date": datetime(2026, 8, 20)},
                      {"source": "finviz", "pe_ratio": 77.19,
                       "snapshot_date": datetime(2026, 8, 11)}])
        assert out["basis"] == pe_basis.BASIS_TTM_EPS
        assert out["reference_basis"] == pe_basis.BASIS_TTM_EPS

    def test_no_reference_at_all_reports_no_reference_basis(self):
        with _periods([]):
            out = pe_basis.resolve_pe(
                "XYZ", price=None,
                rows=[{"source": "yfinance", "pe_ratio": 20.0,
                       "snapshot_date": datetime(2026, 8, 20)}])
        assert out["basis"] == pe_basis.BASIS_NONE
        assert out["reference_basis"] is None
